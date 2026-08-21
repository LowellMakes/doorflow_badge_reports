from __future__ import annotations

import argparse
import base64
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
from urllib import request as urllib_request


DEFAULT_CONFIG = Path(__file__).with_name("config.json")


@dataclass(frozen=True)
class ShopConfig:
    name: str
    doorflow_group_id: str
    captain_email: str


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
class Person:
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    credentials_number: str = ""
    key_fob_number: str = ""
    pin: str = ""
    enabled: str = ""
    system_id: str = ""
    groups: tuple[str, ...] = ()

    @property
    def full_name(self) -> str:
        return (f"{self.first_name} {self.last_name}").strip() or "(unknown)"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_config(path: Path | str = DEFAULT_CONFIG) -> AppConfig:
    raw = _read_json(Path(path))
    shops = tuple(
        ShopConfig(
            name=str(shop["name"]),
            doorflow_group_id=str(shop["doorflow_group_id"]),
            captain_email=str(shop["captain_email"]),
        )
        for shop in raw["shops"]
    )
    api_base = str(raw.get("api_base"))
    sendmail_path = str(raw.get("sendmail_path"))
    from_address = str(raw.get("from_address", ""))
    if not api_base or api_base == "None":
        raise ValueError("config.json must define api_base")
    if not sendmail_path or sendmail_path == "None":
        raise ValueError("config.json must define sendmail_path")
    return AppConfig(
        api_base=api_base,
        sendmail_path=sendmail_path,
        from_address=from_address,
        shops=shops,
    )


def find_shop(config: AppConfig, name: str) -> ShopConfig:
    wanted = name.strip().casefold()
    for shop in config.shops:
        if shop.name.casefold() == wanted:
            return shop
    raise ValueError(f"Unknown shop {name!r}. Available shops: {', '.join(shop.name for shop in config.shops)}")


def previous_month_label(today: dt.date | None = None) -> str:
    today = today or dt.date.today()
    first_of_current_month = today.replace(day=1)
    previous_month_end = first_of_current_month - dt.timedelta(days=1)
    return previous_month_end.strftime("%B %Y")


def _basic_auth_header(auth_key: str) -> str:
    token = base64.b64encode(f"{auth_key}:x".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _decode_people_payload(payload: object) -> list[dict]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("Records", "records", "people", "People", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    raise ValueError(f"Unexpected Doorflow payload: {type(payload)!r}")


def fetch_people(api_base: str, auth_key: str) -> list[dict]:
    url = f"{api_base.rstrip('/')}/people?per_page=1000"
    req = urllib_request.Request(url, headers={"Authorization": _basic_auth_header(auth_key)})
    with urllib_request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return _decode_people_payload(payload)


def _normalize_group_ids(raw_groups: object) -> tuple[str, ...]:
    if not raw_groups:
        return ()
    normalized: list[str] = []
    for item in raw_groups:  # type: ignore[assignment]
        if isinstance(item, dict):
            value = item.get("id")
        else:
            value = item
        if value is not None:
            normalized.append(str(value))
    return tuple(normalized)


def _person_groups(person: dict) -> tuple[str, ...]:
    groups = _normalize_group_ids(person.get("groups"))
    if groups:
        return groups
    return _normalize_group_ids(person.get("group_ids"))


def _coerce_person(person: dict) -> Person:
    return Person(
        first_name=str(person.get("first_name") or person.get("firstName") or person.get("GuessedFirstName") or ""),
        last_name=str(person.get("last_name") or person.get("lastName") or person.get("GuessedLastName") or ""),
        email=str(person.get("email") or person.get("Email") or ""),
        credentials_number=str(person.get("credentials_number") or person.get("credentialsNumber") or person.get("AccessCardId") or ""),
        key_fob_number=str(person.get("key_fob_number") or person.get("keyFobNumber") or person.get("KeyFobNumber") or ""),
        pin=str(person.get("pin") or person.get("Pin") or person.get("AccessPincode") or ""),
        enabled=str(person.get("enabled") if person.get("enabled") is not None else person.get("Enabled", "")),
        system_id=str(person.get("system_id") or person.get("systemId") or person.get("SystemId") or ""),
        groups=_person_groups(person),
    )


def filter_people_by_group(people: Iterable[dict], group_id: str) -> list[dict]:
    wanted = str(group_id)
    filtered = [person for person in people if wanted in _person_groups(person)]
    filtered.sort(key=lambda p: ((p.get("last_name") or p.get("lastName") or "").casefold(), (p.get("first_name") or p.get("firstName") or "").casefold(), (p.get("email") or "").casefold()))
    return filtered


def render_body(*, shop: ShopConfig, period_label: str, recipient: str, people: Sequence[dict]) -> str:
    normalized = [_coerce_person(person) for person in people]
    lines = [
        f"Doorflow badge report for {shop.name}",
        f"Doorflow group id: {shop.doorflow_group_id}",
        f"Period: {period_label}",
        f"Recipient: {recipient}",
        f"Total badges: {len(normalized)}",
        "",
        "Name | Email | Credentials | Fob | PIN | Enabled | System ID",
        "----- | ----- | ----------- | --- | --- | ------- | ---------",
    ]
    for person in normalized:
        lines.append(
            f"{person.full_name} | {person.email or '-'} | {person.credentials_number or '-'} | {person.key_fob_number or '-'} | {person.pin or '-'} | {person.enabled or '-'} | {person.system_id or '-'}"
        )
    return "\n".join(lines) + "\n"


def _people_csv(people: Sequence[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "first_name",
        "last_name",
        "email",
        "credentials_number",
        "key_fob_number",
        "pin",
        "enabled",
        "system_id",
        "groups",
    ])
    for person in (_coerce_person(item) for item in people):
        writer.writerow([
            person.first_name,
            person.last_name,
            person.email,
            person.credentials_number,
            person.key_fob_number,
            person.pin,
            person.enabled,
            person.system_id,
            ";".join(person.groups),
        ])
    return buffer.getvalue()


def build_email(*, subject: str, sender: str, recipient: str, shop: ShopConfig, period_label: str, people: Sequence[dict]) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.set_content(render_body(shop=shop, period_label=period_label, recipient=recipient, people=people))
    attachment_name = f"{shop.name.lower()}-doorflow-badge-report-{period_label.replace(' ', '_').lower()}.csv"
    message.add_attachment(_people_csv(people), subtype="csv", filename=attachment_name)
    return message


def send_via_sendmail(message: EmailMessage, sendmail_path: str) -> None:
    subprocess.run([sendmail_path, "-t", "-oi"], input=message.as_bytes(), check=True)


def _auth_key_from_env() -> str:
    auth_key = os.environ.get("DOORFLOW_AUTH_KEY")
    if not auth_key:
        raise RuntimeError("Set DOORFLOW_AUTH_KEY before running the report.")
    return auth_key


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build and email a monthly Doorflow badge report.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to config.json")
    parser.add_argument("--shop", default=None, help="Shop name to report on")
    parser.add_argument("--recipient", default=None, help="Override the configured recipient")
    parser.add_argument("--subject", default=None, help="Override the email subject")
    parser.add_argument("--period-label", default=None, help="Override the month label")
    parser.add_argument("--dry-run", action="store_true", help="Print the email instead of sending it")
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    shop_name = args.shop or config.default_shop.name
    shop = find_shop(config, shop_name)
    recipient = args.recipient or shop.captain_email
    period_label = args.period_label or previous_month_label()
    subject = args.subject or f"{shop.name} Doorflow badge report - {period_label}"

    auth_key = _auth_key_from_env()
    people = fetch_people(config.api_base, auth_key)
    shop_people = filter_people_by_group(people, shop.doorflow_group_id)
    message = build_email(
        subject=subject,
        sender=config.from_address or recipient,
        recipient=recipient,
        shop=shop,
        period_label=period_label,
        people=shop_people,
    )

    if args.dry_run:
        print(message)
        return 0

    send_via_sendmail(message, config.sendmail_path)
    print(f"Sent {len(shop_people)} badge records for {shop.name} to {recipient}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
