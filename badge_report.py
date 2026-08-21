#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import os
import subprocess
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable, Sequence
from urllib import parse as urllib_parse
from urllib import request as urllib_request

DEFAULT_CONFIG = Path(__file__).with_name("config.json")
DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_ADMIT_EVENT_CODES = (10, 11, 12, 13, 14, 15, 16, 17, 18, 70)


@dataclass(frozen=True)
class ShopConfig:
    name: str
    captain_email: str
    doorflow_channel_name: str | None = None
    doorflow_channel_id: int | None = None

    @property
    def display_door(self) -> str:
        if self.doorflow_channel_name:
            return self.doorflow_channel_name
        if self.doorflow_channel_id is not None:
            return str(self.doorflow_channel_id)
        return self.name


@dataclass(frozen=True)
class AppConfig:
    api_base: str
    sendmail_path: str
    from_address: str
    shops: tuple[ShopConfig, ...]

    @property
    def default_shop(self) -> ShopConfig:
        if not self.shops:
            raise ValueError("config.json must define at least one shop")
        return self.shops[0]


@dataclass(frozen=True)
class BadgeSummary:
    person_id: int | None
    person_name: str
    credentials_number: str
    first_seen: dt.datetime
    last_seen: dt.datetime
    event_count: int

    @property
    def display_name(self) -> str:
        if self.person_name:
            return self.person_name
        if self.person_id is not None:
            return f"Person {self.person_id}"
        return "(unknown)"


@dataclass(frozen=True)
class EventRecord:
    person_id: int | None
    person_name: str
    credentials_number: str
    door_controller_id: int | None
    door_controller_name: str
    created_at: dt.datetime
    event_code: int | None
    event_label: str


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_config(path: Path | str = DEFAULT_CONFIG) -> AppConfig:
    raw = _read_json(Path(path))
    shops: list[ShopConfig] = []
    for shop in raw["shops"]:
        doorflow_channel_id = shop.get("doorflow_channel_id")
        if doorflow_channel_id in ("", None):
            parsed_channel_id = None
        else:
            parsed_channel_id = int(doorflow_channel_id)
        shops.append(
            ShopConfig(
                name=str(shop["name"]),
                captain_email=str(shop["captain_email"]),
                doorflow_channel_name=(str(shop["doorflow_channel_name"]) if shop.get("doorflow_channel_name") else None),
                doorflow_channel_id=parsed_channel_id,
            )
        )

    api_base = str(raw.get("api_base") or "").strip()
    sendmail_path = str(raw.get("sendmail_path") or "").strip()
    from_address = str(raw.get("from_address") or "").strip()
    if not api_base:
        raise ValueError("config.json must define api_base")
    if not sendmail_path:
        raise ValueError("config.json must define sendmail_path")
    return AppConfig(
        api_base=api_base,
        sendmail_path=sendmail_path,
        from_address=from_address,
        shops=tuple(shops),
    )


def find_shop(config: AppConfig, name: str) -> ShopConfig:
    wanted = name.strip().casefold()
    for shop in config.shops:
        if shop.name.casefold() == wanted:
            return shop
    raise ValueError(
        f"Unknown shop {name!r}. Available shops: {', '.join(shop.name for shop in config.shops)}"
    )


def previous_month_label(today: dt.date | None = None) -> str:
    today = today or dt.date.today()
    first_of_current_month = today.replace(day=1)
    previous_month_end = first_of_current_month - dt.timedelta(days=1)
    return previous_month_end.strftime("%B %Y")


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _format_dt(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _parse_dt(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _auth_token_from_env() -> str:
    for key in ("DOORFLOW_ACCESS_TOKEN", "DOORFLOW_AUTH_TOKEN", "DOORFLOW_AUTH_KEY"):
        token = os.environ.get(key)
        if token:
            return token
    raise RuntimeError(
        "Set DOORFLOW_ACCESS_TOKEN (preferred) or DOORFLOW_AUTH_TOKEN/DOORFLOW_AUTH_KEY before running the report."
    )


def _authorization_header(token: str) -> str:
    return f"Bearer {token}"


def _request_json(url: str, token: str) -> object:
    req = urllib_request.Request(url, headers={"Authorization": _authorization_header(token)})
    with urllib_request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _decode_list_payload(payload: object) -> list[dict]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("Records", "records", "events", "Events", "people", "People", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    raise ValueError(f"Unexpected DoorFlow payload: {type(payload)!r}")


def list_channels(api_base: str, token: str, page_size: int = 1000) -> list[dict]:
    channels: list[dict] = []
    page = 1
    while True:
        query = urllib_parse.urlencode({"n": page_size, "page": page})
        url = f"{api_base.rstrip('/')}/channels?{query}"
        payload = _request_json(url, token)
        items = _decode_list_payload(payload)
        if not items:
            break
        channels.extend(items)
        if len(items) < page_size:
            break
        page += 1
    return channels


def resolve_channel_id(config: AppConfig, shop: ShopConfig, token: str) -> int:
    if shop.doorflow_channel_id is not None:
        return shop.doorflow_channel_id

    if not shop.doorflow_channel_name:
        raise ValueError(
            f"Shop {shop.name!r} must define either doorflow_channel_id or doorflow_channel_name in config.json"
        )

    wanted = shop.doorflow_channel_name.casefold()
    channels = list_channels(config.api_base, token)
    matches = [
        channel
        for channel in channels
        if str(channel.get("name") or channel.get("channel_name") or "").casefold() == wanted
    ]
    if not matches:
        available = ", ".join(
            str(channel.get("name") or channel.get("channel_name") or channel.get("id"))
            for channel in channels
        )
        raise ValueError(
            f"No DoorFlow channel matched {shop.doorflow_channel_name!r}. Available channels: {available}"
        )
    return int(matches[0]["id"])


def fetch_events(
    api_base: str,
    token: str,
    channel_id: int,
    since: dt.datetime,
    event_codes: Sequence[int] = DEFAULT_ADMIT_EVENT_CODES,
    page_size: int = 1000,
) -> list[dict]:
    since_text = since.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    code_text = ",".join(str(code) for code in event_codes)
    events: list[dict] = []
    page = 1
    while True:
        query = urllib_parse.urlencode(
            {
                "door_controllers": channel_id,
                "since": since_text,
                "event_codes": code_text,
                "n": page_size,
                "page": page,
                "sort": "DESC",
            },
            safe=",",
        )
        url = f"{api_base.rstrip('/')}/events?{query}"
        payload = _request_json(url, token)
        items = _decode_list_payload(payload)
        if not items:
            break
        events.extend(items)
        if len(items) < page_size:
            break
        page += 1
    return events


def _to_event_record(event: dict) -> EventRecord:
    created_at_raw = event.get("created_at") or event.get("timestamp")
    if not created_at_raw:
        raise ValueError(f"Event missing created_at: {event!r}")
    return EventRecord(
        person_id=(int(event["person_id"]) if event.get("person_id") not in (None, "") else None),
        person_name=str(event.get("person_name") or event.get("person") or ""),
        credentials_number=str(event.get("credentials_number") or event.get("credential_number") or ""),
        door_controller_id=(int(event["door_controller_id"]) if event.get("door_controller_id") not in (None, "") else None),
        door_controller_name=str(event.get("door_controller_name") or event.get("channel_name") or ""),
        created_at=_parse_dt(str(created_at_raw)),
        event_code=(int(event["event_code"]) if event.get("event_code") not in (None, "") else None),
        event_label=str(event.get("event_label") or ""),
    )


def summarize_badge_events(events: Iterable[dict]) -> list[BadgeSummary]:
    buckets: dict[str, dict[str, object]] = {}
    for raw_event in events:
        event = _to_event_record(raw_event)
        key = str(event.person_id) if event.person_id is not None else event.person_name or event.credentials_number
        if not key:
            continue
        bucket = buckets.get(key)
        if bucket is None:
            buckets[key] = {
                "person_id": event.person_id,
                "person_name": event.person_name,
                "credentials_number": event.credentials_number,
                "first_seen": event.created_at,
                "last_seen": event.created_at,
                "event_count": 1,
            }
            continue
        bucket["person_name"] = bucket["person_name"] or event.person_name
        bucket["credentials_number"] = bucket["credentials_number"] or event.credentials_number
        bucket["first_seen"] = min(bucket["first_seen"], event.created_at)  # type: ignore[arg-type]
        bucket["last_seen"] = max(bucket["last_seen"], event.created_at)  # type: ignore[arg-type]
        bucket["event_count"] = int(bucket["event_count"]) + 1

    summaries = [
        BadgeSummary(
            person_id=bucket["person_id"],
            person_name=str(bucket["person_name"]),
            credentials_number=str(bucket["credentials_number"]),
            first_seen=bucket["first_seen"],  # type: ignore[arg-type]
            last_seen=bucket["last_seen"],  # type: ignore[arg-type]
            event_count=int(bucket["event_count"]),
        )
        for bucket in buckets.values()
    ]
    summaries.sort(key=lambda item: (item.last_seen, item.display_name.casefold()), reverse=True)
    return summaries


def render_body(
    *,
    shop: ShopConfig,
    period_label: str,
    recipient: str,
    summaries: Sequence[BadgeSummary],
) -> str:
    total_events = sum(summary.event_count for summary in summaries)
    lines = [
        f"Doorflow badge report for {shop.name}",
        f"Door: {shop.display_door}",
        f"Period: {period_label}",
        f"Recipient: {recipient}",
        f"Unique people: {len(summaries)}",
        f"Total badge events: {total_events}",
        "",
        "Name | Person ID | Credentials | First badge | Last badge | Badge count",
        "----- | --------- | ----------- | ----------- | ---------- | -----------",
    ]
    for summary in summaries:
        lines.append(
            f"{summary.display_name} | {summary.person_id if summary.person_id is not None else '-'} | {summary.credentials_number or '-'} | {_format_dt(summary.first_seen)} | {_format_dt(summary.last_seen)} | {summary.event_count}"
        )
    return "\n".join(lines) + "\n"


def _summaries_csv(summaries: Sequence[BadgeSummary]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "person_id",
        "person_name",
        "credentials_number",
        "first_seen_utc",
        "last_seen_utc",
        "badge_count",
    ])
    for summary in summaries:
        writer.writerow(
            [
                summary.person_id if summary.person_id is not None else "",
                summary.person_name,
                summary.credentials_number,
                _format_dt(summary.first_seen),
                _format_dt(summary.last_seen),
                summary.event_count,
            ]
        )
    return buffer.getvalue()


def build_email(
    *,
    subject: str,
    sender: str,
    recipient: str,
    shop: ShopConfig,
    period_label: str,
    summaries: Sequence[BadgeSummary],
) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.set_content(
        render_body(
            shop=shop,
            period_label=period_label,
            recipient=recipient,
            summaries=summaries,
        )
    )
    attachment_name = f"{shop.name.lower()}-doorflow-badge-report-{period_label.replace(' ', '_').lower()}.csv"
    message.add_attachment(_summaries_csv(summaries), subtype="csv", filename=attachment_name)
    return message


def send_via_sendmail(message: EmailMessage, sendmail_path: str) -> None:
    subprocess.run([sendmail_path, "-t", "-oi"], input=message.as_bytes(), check=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build and email a DoorFlow badge report for a single door.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to config.json")
    parser.add_argument("--shop", default=None, help="Shop name to report on")
    parser.add_argument("--recipient", default=None, help="Override the configured recipient")
    parser.add_argument("--subject", default=None, help="Override the email subject")
    parser.add_argument("--period-label", default=None, help="Override the period label")
    parser.add_argument("--days", type=int, default=DEFAULT_LOOKBACK_DAYS, help="Look back this many days")
    parser.add_argument("--dry-run", action="store_true", help="Print the email instead of sending it")
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    shop_name = args.shop or config.default_shop.name
    shop = find_shop(config, shop_name)
    recipient = args.recipient or shop.captain_email
    period_label = args.period_label or f"Last {args.days} Days"
    subject = args.subject or f"{shop.name} Doorflow badge report - {period_label}"

    token = _auth_token_from_env()
    channel_id = resolve_channel_id(config, shop, token)
    since = _utc_now() - dt.timedelta(days=args.days)
    events = fetch_events(config.api_base, token, channel_id, since)
    summaries = summarize_badge_events(events)
    message = build_email(
        subject=subject,
        sender=config.from_address or recipient,
        recipient=recipient,
        shop=shop,
        period_label=period_label,
        summaries=summaries,
    )

    if args.dry_run:
        print(message)
        return 0

    send_via_sendmail(message, config.sendmail_path)
    print(f"Sent {len(summaries)} badge summaries for {shop.name} to {recipient}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
