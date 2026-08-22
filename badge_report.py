#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import os
import socket
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
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
    report_every_days: int = 30
    report_summary: "ReportSummaryConfig" = field(default_factory=lambda: ReportSummaryConfig())

    @property
    def display_door(self) -> str:
        if self.doorflow_channel_name:
            return self.doorflow_channel_name
        if self.doorflow_channel_id is not None:
            return str(self.doorflow_channel_id)
        return self.name


@dataclass(frozen=True)
class ReportSummaryConfig:
    enabled: bool = True
    show_total: bool = True
    show_status_counts: bool = True
    show_unique_people: bool = True
    show_average_per_day: bool = True
    show_busiest_day: bool = True
    show_top_accepted_people: bool = True
    show_top_rejected_people: bool = True
    top_n: int = 5


@dataclass(frozen=True)
class AppConfig:
    api_base: str
    sendmail_path: str
    from_address: str
    default_email: str
    state_path: Path
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


def _parse_summary_config(raw: object | None) -> ReportSummaryConfig:
    if raw in (None, ""):
        return ReportSummaryConfig()
    if not isinstance(raw, dict):
        raise ValueError("shop summary config must be a mapping")
    return ReportSummaryConfig(
        enabled=bool(raw.get("enabled", True)),
        show_total=bool(raw.get("show_total", True)),
        show_status_counts=bool(raw.get("show_status_counts", True)),
        show_unique_people=bool(raw.get("show_unique_people", True)),
        show_average_per_day=bool(raw.get("show_average_per_day", True)),
        show_busiest_day=bool(raw.get("show_busiest_day", True)),
        show_top_accepted_people=bool(raw.get("show_top_accepted_people", True)),
        show_top_rejected_people=bool(raw.get("show_top_rejected_people", True)),
        top_n=int(raw.get("top_n", 5)),
    )


def load_config(path: Path | str = DEFAULT_CONFIG) -> AppConfig:
    config_path = Path(path)
    raw = _read_json(config_path)
    shops: list[ShopConfig] = []
    for shop in raw["shops"]:
        doorflow_channel_id = shop.get("doorflow_channel_id")
        if doorflow_channel_id in ("", None):
            parsed_channel_id = None
        else:
            parsed_channel_id = int(doorflow_channel_id)
        summary = _parse_summary_config(shop.get("summary") or shop.get("report_summary"))
        report_every_days = int(shop.get("report_every_days", 30))
        shops.append(
            ShopConfig(
                name=str(shop["name"]),
                captain_email=str(shop["captain_email"]),
                doorflow_channel_name=(str(shop["doorflow_channel_name"]) if shop.get("doorflow_channel_name") else None),
                doorflow_channel_id=parsed_channel_id,
                report_every_days=report_every_days,
                report_summary=summary,
            )
        )

    api_base = str(raw.get("api_base") or "").strip()
    sendmail_path = str(raw.get("sendmail_path") or "").strip()
    from_address = str(raw.get("from_address") or "").strip()
    default_email = str(raw.get("default_email") or raw.get("default_recipient_email") or from_address or "").strip()
    state_path_raw = raw.get("state_path")
    if state_path_raw in (None, ""):
        state_path = config_path.with_name("badge_report_state.json")
    else:
        state_path = Path(state_path_raw)
        if not state_path.is_absolute():
            state_path = config_path.parent / state_path
    if not api_base:
        raise ValueError("config.json must define api_base")
    if not sendmail_path:
        raise ValueError("config.json must define sendmail_path")
    if not from_address:
        raise ValueError("config.json must define from_address")
    if not default_email:
        raise ValueError("config.json must define default_email")
    return AppConfig(
        api_base=api_base,
        sendmail_path=sendmail_path,
        from_address=from_address,
        default_email=default_email,
        state_path=state_path,
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


def _channel_name(channel: dict) -> str:
    return str(channel.get("name") or channel.get("channel_name") or channel.get("doorflow_channel_name") or "")


def _channel_id_candidates(channel: dict) -> list[int]:
    candidates: list[int] = []
    for key in ("id", "channel_id", "channelId", "doorflow_channel_id"):
        value = channel.get(key)
        if value in (None, ""):
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed not in candidates:
            candidates.append(parsed)
    return candidates


def resolve_channel_id(config: AppConfig, shop: ShopConfig, token: str) -> int:
    channels = list_channels(config.api_base, token)

    if shop.doorflow_channel_name:
        wanted = shop.doorflow_channel_name.casefold()
        matches = [channel for channel in channels if _channel_name(channel).casefold() == wanted]
        if not matches:
            available = ", ".join(_channel_name(channel) or str(channel.get("id")) for channel in channels)
            raise ValueError(
                f"No DoorFlow channel matched {shop.doorflow_channel_name!r}. Available channels: {available}"
            )
        resolved = _channel_id_candidates(matches[0])
        if not resolved:
            raise ValueError(
                f"DoorFlow channel {shop.doorflow_channel_name!r} was found but has no numeric id field"
            )
        return resolved[0]

    if shop.doorflow_channel_id is not None:
        wanted_id = int(shop.doorflow_channel_id)
        matches = [channel for channel in channels if wanted_id in _channel_id_candidates(channel)]
        if not matches:
            available = ", ".join(_channel_name(channel) or str(channel.get("id")) for channel in channels)
            raise ValueError(
                f"No DoorFlow channel matched id {wanted_id!r}. Available channels: {available}"
            )
        return wanted_id

    raise ValueError(
        f"Shop {shop.name!r} must define either doorflow_channel_id or doorflow_channel_name in config.json"
    )


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


def _person_counts(events: Sequence[BadgeEvent], *, status: str | None = None) -> tuple[Counter[str], dict[str, str]]:
    counts: Counter[str] = Counter()
    display_names: dict[str, str] = {}
    for event in events:
        if status is not None and event.status != status:
            continue
        key = event.display_name.casefold()
        counts[key] += 1
        display_names.setdefault(key, event.display_name)
    return counts, display_names


def _top_people(events: Sequence[BadgeEvent], *, status: str, top_n: int) -> list[tuple[str, int]]:
    counts, display_names = _person_counts(events, status=status)
    ordered = sorted(
        counts.items(),
        key=lambda item: (-item[1], display_names[item[0]].casefold()),
    )
    return [(display_names[key], count) for key, count in ordered[:top_n]]


def _format_bullet_lines(title: str, items: Sequence[str]) -> list[str]:
    lines = [title]
    for item in items:
        lines.append(f"  - {item}")
    if not items:
        lines.append("  - None")
    return lines


def _unique_addresses(addresses: Sequence[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for address in addresses:
        normalized = address.strip()
        if not normalized:
            continue
        key = normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(normalized)
    return unique


@dataclass(frozen=True)
class ShopReportPlan:
    shop: ShopConfig
    recipients: tuple[str, ...]
    since: dt.datetime
    period_days: float
    last_sent_at: dt.datetime | None


def load_state(path: Path | str) -> dict[str, dt.datetime]:
    state_path = Path(path)
    if not state_path.exists():
        return {}
    raw = _read_json(state_path)
    entries = raw.get("last_sent_at", raw)
    if not isinstance(entries, dict):
        raise ValueError("state file must contain a mapping of shop names to timestamps")
    state: dict[str, dt.datetime] = {}
    for shop_name, timestamp in entries.items():
        if timestamp in (None, ""):
            continue
        state[str(shop_name)] = _parse_dt(str(timestamp))
    return state


def save_state(path: Path | str, state: dict[str, dt.datetime]) -> None:
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_sent_at": {
            shop_name: timestamp.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
            for shop_name, timestamp in sorted(state.items())
        }
    }
    state_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _report_window_start(last_sent_at: dt.datetime | None, report_every_days: int, now: dt.datetime) -> dt.datetime:
    if last_sent_at is not None:
        return last_sent_at
    return now - dt.timedelta(days=report_every_days)


def _is_report_due(last_sent_at: dt.datetime | None, report_every_days: int, now: dt.datetime) -> bool:
    if last_sent_at is None:
        return True
    return (now - last_sent_at) >= dt.timedelta(days=report_every_days)


def _period_days_since(since: dt.datetime, now: dt.datetime) -> float:
    return max((now - since).total_seconds() / 86400.0, 1.0)


def plan_shop_reports(
    config: AppConfig,
    state: dict[str, dt.datetime],
    now: dt.datetime,
    *,
    shop_name: str | None = None,
    force: bool = False,
    days_override: int | None = None,
    recipient_override: str | None = None,
) -> list[ShopReportPlan]:
    if shop_name:
        selected_shops = [find_shop(config, shop_name)]
    else:
        selected_shops = list(config.shops)

    plans: list[ShopReportPlan] = []
    for shop in selected_shops:
        interval_days = int(days_override or shop.report_every_days)
        last_sent_at = state.get(shop.name)
        due = force or _is_report_due(last_sent_at, interval_days, now)
        if not due:
            continue
        since = _report_window_start(last_sent_at, interval_days, now)
        recipients = _unique_addresses([config.default_email, recipient_override or shop.captain_email])
        plans.append(
            ShopReportPlan(
                shop=shop,
                recipients=tuple(recipients),
                since=since,
                period_days=_period_days_since(since, now),
                last_sent_at=last_sent_at,
            )
        )
    return plans


def _default_period_label(period_days: float) -> str:
    days = max(1, int(round(period_days)))
    return f"Last {days} Day" if days == 1 else f"Last {days} Days"


def build_summary_lines(
    *,
    events: Sequence[BadgeEvent],
    period_days: int,
    summary_config: ReportSummaryConfig,
) -> list[str]:
    if not summary_config.enabled:
        return []

    total = len(events)
    accepted = sum(1 for event in events if event.status == "Accepted")
    rejected = sum(1 for event in events if event.status == "Rejected")
    unique_people = len({event.display_name.casefold() for event in events})
    average_per_day = (total / period_days) if period_days > 0 else 0.0
    by_day = Counter(event.created_at_eastern.date() for event in events)
    busiest_day = None
    busiest_count = 0
    if by_day:
        busiest_day, busiest_count = sorted(by_day.items(), key=lambda item: (-item[1], item[0]))[0]

    lines = ["Summary:"]
    if summary_config.show_total:
        lines.append(f"  Total badge events: {total}")
    if summary_config.show_status_counts:
        lines.append(f"  Accepted: {accepted}")
        lines.append(f"  Rejected: {rejected}")
    if summary_config.show_unique_people:
        lines.append(f"  Unique badge holders: {unique_people}")
    if summary_config.show_average_per_day:
        lines.append(f"  Average badges per day: {average_per_day:.1f}")
    if summary_config.show_busiest_day:
        if busiest_day is None:
            lines.append("  Busiest day: None")
        else:
            lines.append(f"  Busiest day: {busiest_day.isoformat()} ({busiest_count} events)")
    if summary_config.show_top_accepted_people:
        lines.extend(_format_bullet_lines(
            f"Top {summary_config.top_n} accepted badge holders:",
            [f"{name} ({count})" for name, count in _top_people(events, status="Accepted", top_n=summary_config.top_n)],
        ))
    if summary_config.show_top_rejected_people:
        lines.extend(_format_bullet_lines(
            f"Top {summary_config.top_n} rejected attempts:",
            [f"{name} ({count})" for name, count in _top_people(events, status="Rejected", top_n=summary_config.top_n)],
        ))
    return lines


def _report_footer_lines(now: dt.datetime | None = None, hostname: str | None = None, script_name: str | None = None) -> list[str]:
    stamp = (now or _utc_now()).astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S %Z")
    host = hostname or socket.gethostname()
    script = str(Path(script_name or sys.argv[0]).resolve())
    return [
        "",
        f"Generated: {stamp}",
        f"Hostname: {host}",
        f"Script: {script}",
        "Source: https://github.com/LowellMakes/doorflow_badge_reports",
    ]


def render_body(
    *,
    shop: ShopConfig,
    period_label: str,
    events: Sequence[BadgeEvent],
    period_days: int,
) -> str:
    lines = [
        f"Doorflow badge report for {shop.name}",
        f"Door: {shop.display_door}",
        f"Period: {period_label}",
    ]
    lines.extend(build_summary_lines(events=events, period_days=period_days, summary_config=shop.report_summary))
    lines.extend([
        "",
        "Date/Time Eastern | Accepted/Rejected | Person | Fob #",
        "----------------- | ----------------- | ------ | -----",
    ])
    for event in events:
        lines.append(
            f"{_format_eastern(event.created_at)} | {event.status} | {event.display_name} | {event.credentials_number or '-'}"
        )
    lines.extend(_report_footer_lines())
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
    recipients: Sequence[str],
    shop: ShopConfig,
    period_label: str,
    events: Sequence[BadgeEvent],
    period_days: int,
) -> EmailMessage:
    recipient_list = _unique_addresses(recipients)
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(recipient_list)
    message.set_content(
        render_body(
            shop=shop,
            period_label=period_label,
            events=events,
            period_days=period_days,
        )
    )
    attachment_name = f"{shop.name.lower()}-doorflow-badge-report-{period_label.replace(' ', '_').lower()}.csv"
    message.add_attachment(_events_csv(events), subtype="csv", filename=attachment_name)
    return message


def send_via_sendmail(message: EmailMessage, sendmail_path: str, envelope_from: str) -> None:
    subprocess.run([sendmail_path, "-f", envelope_from, "-t", "-oi"], input=message.as_bytes(), check=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build and email DoorFlow badge reports for configured shops.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to config.json")
    parser.add_argument("--shop", default=None, help="Shop name to report on")
    parser.add_argument("--recipient", default=None, help="Override the configured recipient")
    parser.add_argument("--subject", default=None, help="Override the email subject")
    parser.add_argument("--period-label", default=None, help="Override the period label")
    parser.add_argument("--days", type=int, default=None, help="Override the lookback days for this run")
    parser.add_argument("--force", action="store_true", help="Send even if the report is not yet due")
    parser.add_argument("--dry-run", action="store_true", help="Print the email instead of sending it")
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    now = _utc_now()
    state = load_state(config.state_path)
    plans = plan_shop_reports(
        config,
        state,
        now,
        shop_name=args.shop,
        force=args.force,
        days_override=args.days,
        recipient_override=args.recipient,
    )
    if not plans:
        print("No badge reports are due right now.")
        return 0

    token = _auth_token_from_env()
    updated_state = dict(state)
    had_errors = False
    for plan in plans:
        try:
            channel_id = resolve_channel_id(config, plan.shop, token)
            events = fetch_events(config.api_base, token, channel_id, plan.since)
            badge_events = collect_badge_events(events)
            period_label = args.period_label or _default_period_label(plan.period_days)
            subject = args.subject or f"{plan.shop.name} Doorflow badge report - {period_label}"
            message = build_email(
                subject=subject,
                sender=config.from_address,
                recipients=plan.recipients,
                shop=plan.shop,
                period_label=period_label,
                events=badge_events,
                period_days=plan.period_days,
            )
            if args.dry_run:
                print(f"=== {plan.shop.name} ===")
                print(message)
            else:
                send_via_sendmail(message, config.sendmail_path, config.from_address)
                print(
                    f"Sent {len(badge_events)} badge events for {plan.shop.name} to {', '.join(plan.recipients)}"
                )
            updated_state[plan.shop.name] = now
        except Exception as exc:
            had_errors = True
            print(f"Error processing {plan.shop.name}: {exc}", file=sys.stderr)

    if not args.dry_run and updated_state != state:
        save_state(config.state_path, updated_state)

    return 1 if had_errors else 0


if __name__ == "__main__":
    raise SystemExit(run())
