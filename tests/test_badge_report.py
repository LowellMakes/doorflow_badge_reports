from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import badge_report as report


class BadgeReportTests(unittest.TestCase):
    def test_load_config_and_shop_lookup(self) -> None:
        with TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            cfg.write_text(
                """
                {
                  "api_base": "https://api.doorflow.com/api/3",
                  "sendmail_path": "/usr/sbin/sendmail",
                  "from_address": "sender@example.invalid",
                  "shops": [
                    {
                      "name": "Woodshop",
                      "doorflow_channel_name": "Woodshop",
                      "captain_email": "sender@example.invalid"
                    }
                  ]
                }
                """,
                encoding="utf-8",
            )
            config = report.load_config(cfg)
            shop = report.find_shop(config, "woodshop")
            self.assertEqual(config.api_base, "https://api.doorflow.com/api/3")
            self.assertEqual(shop.doorflow_channel_name, "Woodshop")
            self.assertEqual(shop.captain_email, "sender@example.invalid")

    def test_resolve_channel_id_uses_channel_name(self) -> None:
        shop = report.ShopConfig(
            name="Woodshop",
            captain_email="sender@example.invalid",
            doorflow_channel_name="Woodshop Door",
        )
        config = report.AppConfig(
            api_base="https://api.doorflow.com/api/3",
            sendmail_path="/usr/sbin/sendmail",
            from_address="sender@example.invalid",
            shops=(shop,),
        )
        with patch.object(
            report,
            "list_channels",
            return_value=[
                {"id": 4622, "name": "Woodshop Door"},
                {"id": 4695, "name": "ChopShop"},
            ],
        ):
            self.assertEqual(report.resolve_channel_id(config, shop, "token"), 4622)

    def test_summarize_badge_events_deduplicates_people_and_tracks_dates(self) -> None:
        events = [
            {
                "person_id": 101,
                "person_name": "Ada Lovelace",
                "credentials_number": "1234",
                "door_controller_id": 4622,
                "door_controller_name": "Woodshop",
                "event_code": 10,
                "event_label": "Ada admitted using card",
                "created_at": "2026-07-01T12:00:00Z",
            },
            {
                "person_id": 101,
                "person_name": "Ada Lovelace",
                "credentials_number": "1234",
                "door_controller_id": 4622,
                "door_controller_name": "Woodshop",
                "event_code": 10,
                "event_label": "Ada admitted using card",
                "created_at": "2026-07-10T15:30:00Z",
            },
            {
                "person_id": 202,
                "person_name": "Grace Hopper",
                "credentials_number": "9876",
                "door_controller_id": 4622,
                "door_controller_name": "Woodshop",
                "event_code": 10,
                "event_label": "Grace admitted using card",
                "created_at": "2026-07-09T08:45:00Z",
            },
        ]

        summaries = report.summarize_badge_events(events)
        self.assertEqual([summary.person_name for summary in summaries], ["Ada Lovelace", "Grace Hopper"])

        ada = summaries[0]
        self.assertEqual(ada.person_id, 101)
        self.assertEqual(ada.event_count, 2)
        self.assertEqual(ada.first_seen, datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(ada.last_seen, datetime(2026, 7, 10, 15, 30, tzinfo=timezone.utc))

    def test_build_email_contains_body_and_csv_attachment(self) -> None:
        shop = report.ShopConfig(
            name="Woodshop",
            captain_email="sender@example.invalid",
            doorflow_channel_name="Woodshop",
        )
        summaries = [
            report.BadgeSummary(
                person_id=101,
                person_name="Ada Lovelace",
                credentials_number="1234",
                first_seen=datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc),
                last_seen=datetime(2026, 7, 10, 15, 30, tzinfo=timezone.utc),
                event_count=2,
            )
        ]
        message = report.build_email(
            subject="Woodshop badge report",
            sender="sender@example.invalid",
            recipient="sender@example.invalid",
            shop=shop,
            period_label="Last 30 Days",
            summaries=summaries,
        )

        self.assertIsInstance(message, EmailMessage)
        self.assertEqual(message["Subject"], "Woodshop badge report")
        self.assertEqual(message["To"], "sender@example.invalid")
        body = message.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("Doorflow badge report for Woodshop", body)
        self.assertIn("Ada Lovelace", body)
        attachment_text = message.get_payload()[1].get_content()
        self.assertIn("person_id,person_name,credentials_number,first_seen_utc,last_seen_utc,badge_count", attachment_text)
        self.assertIn("Ada Lovelace", attachment_text)


if __name__ == "__main__":
    unittest.main()
