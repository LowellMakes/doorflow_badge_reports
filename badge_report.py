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
DEFAULT_REJECT_EVENT_CODES = (20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 71, 72, 73)
DEFAULT_ACCESS_EVENT_CODES = DEFAULT_ADMIT_EVENT_CODES + DEFAULT_REJECT_EVENT_CODES


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
class BadgeEvent:
    created_at: dt.datetime
    person_name: str
    credentials_number: str
    status: str

    @property
    def created_at_eastern(self) -> dt.datetime:
        return self.created_at.astimezone(_eastern_tz_for(self.created_at))

    @property
    def display_name(self) -> str:
        return self.person_name or "(unknown)"


@dataclass(frozen=True)
class EventRecord:
    person_name: str
    credentials_number: str
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
    if not from_address:
        raise ValueError("config.json must define from_address")
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


def _us_dst_bounds_utc(year: int) -> tuple[dt.datetime, dt.datetime]:
    def nth_weekday(month: int, weekday: int, n: int) -> dt.date:
        first = dt.date(year, month, 1)
        days_until_weekday = (weekday - first.weekday()) % 7
        return first + dt.timedelta(days=days_until_weekday + (n - 1) * 7)

    dst_start = nth_weekday(3, 6, 2)  # second Sunday in March
    dst_end = nth_weekday(11, 6, 1)   # first Sunday in November
    return (
        dt.datetime(year, 3, dst_start.day, 7, 0, tzinfo=dt.timezone.utc),
        dt.datetime(year, 11, dst_end.day, 6, 0, tzinfo=dt.timezone.utc),
    )


def _eastern_tz_for(value: dt.datetime) -> dt.tzinfo:
    start_utc, end_utc = _us_dst_bounds_utc(value.year)
    is_dst = start_utc <= value < end_utc
    offset = dt.timedelta(hours=-4 if is_dst else -5)
    return dt.timezone(offset, "EDT" if is_dst else "EST")


def _format_eastern(value: dt.datetime) -> str:
    return value.astimezone(_eastern_tz_for(value)).strftime("%Y-%m-%d %H:%M:%S %Z")


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
    event_codes: Sequence[int] = DEFAULT_ACCESS_EVENT_CODES,
    page_size: int = 1000,
) -> list[dict]:
    since_text = since.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    code_set = {int(code) for code in event_codes}
    events: list[dict] = []
    page = 1
    while True:
        query = urllib_parse.urlencode(
            {
                "channels": channel_id,
                "since": since_text,
                "n": page_size,
                "page": page,
                "sort": "DESC",
            }
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

    if not code_set:
        return events
    filtered: list[dict] = []
    for event in events:
        try:
            code = int(event.get("event_code"))
        except (TypeError, ValueError):
            continue
        if code in code_set:
            filtered.append(event)
    return filtered


def _to_event_record(event: dict) -> EventRecord:
    created_at_raw = event.get("created_at") or event.get("timestamp")
    if not created_at_raw:
        raise ValueError(f"Event missing created_at: {event!r}")
    return EventRecord(
        person_name=str(event.get("person_name") or event.get("person") or ""),
        credentials_number=str(event.get("credentials_number") or event.get("credential_number") or ""),
        created_at=_parse_dt(str(created_at_raw)),
        event_code=(int(event["event_code"]) if event.get("event_code") not in (None, "") else None),
        event_label=str(event.get("event_label") or ""),
    )


def _event_status(event_code: int | None) -> str:
    if event_code in DEFAULT_ADMIT_EVENT_CODES:
        return "Accepted"
    if event_code in DEFAULT_REJECT_EVENT_CODES:
        return "Rejected"
    return "Unknown"


def collect_badge_events(events: Iterable[dict]) -> list[BadgeEvent]:
    collected: list[BadgeEvent] = []
    for raw_event in events:
        event = _to_event_record(raw_event)
        collected.append(
            BadgeEvent(
                created_at=event.created_at,
                person_name=event.person_name,
                credentials_number=event.credentials_number,
                status=_event_status(event.event_code),
            )
        )
    collected.sort(key=lambda item: item.created_at)
    return collected


def render_body(
    *,
    shop: ShopConfig,
    period_label: str,
    recipient: str,
    events: Sequence[BadgeEvent],
) -> str:
    lines = [
        f"Doorflow badge report for {shop.name}",
        f"Door: {shop.display_door}",
        f"Period: {period_label}",
        f"Recipient: {recipient}",
        f"Total badge events: {len(events)}",
        "",
        "Date/Time Eastern | Accepted/Rejected | Person | Fob #",
        "----------------- | ----------------- | ------ | -----",
    ]
    for event in events:
        lines.append(
            f"{_format_eastern(event.created_at)} | {event.status} | {event.display_name} | {event.credentials_number or '-'}"
        )
    return "\n".join(lines) + "\n"


def _events_csv(events: Sequence[BadgeEvent]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "date_time_eastern",
        "accepted_rejected",
        "person_name",
        "fob_number",
    ])
    for event in events:
        writer.writerow([
            _format_eastern(event.created_at),
            event.status,
            event.display_name,
            event.credentials_number,
        ])
    return buffer.getvalue()


def build_email(
    *,
    subject: str,
    sender: str,
    recipient: str,
    shop: ShopConfig,
    period_label: str,
    events: Sequence[BadgeEvent],
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
            events=events,
        )
    )
    attachment_name = f"{shop.name.lower()}-doorflow-badge-report-{period_label.replace(' ', '_').lower()}.csv"
    message.add_attachment(_events_csv(events), subtype="csv", filename=attachment_name)
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
    badge_events = collect_badge_events(events)
    message = build_email(
        subject=subject,
        sender=config.from_address,
        recipient=recipient,
        shop=shop,
        period_label=period_label,
        events=badge_events,
    )

    if args.dry_run:
        print(message)
        return 0

    send_via_sendmail(message, config.sendmail_path)
    print(f"Sent {len(badge_events)} badge events for {shop.name} to {recipient}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
