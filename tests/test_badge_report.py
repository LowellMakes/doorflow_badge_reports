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
                  "default_email": "sender@example.invalid",
                  "shops": [
                    {
                      "name": "Woodshop",
                      "doorflow_channel_name": "Wood Shop Door",
                      "captain_email": "captain@example.invalid",
                      "summary": {
                        "enabled": false,
                        "top_n": 3,
                        "show_average_per_day": false
                      }
                    }
                  ]
                }
                """,
                encoding="utf-8",
            )
            config = report.load_config(cfg)
            shop = report.find_shop(config, "woodshop")
            self.assertEqual(config.api_base, "https://api.doorflow.com/api/3")
            self.assertEqual(shop.doorflow_channel_name, "Wood Shop Door")
            self.assertEqual(shop.captain_email, "captain@example.invalid")
            self.assertEqual(config.default_email, "sender@example.invalid")
            self.assertFalse(shop.report_summary.enabled)
            self.assertEqual(shop.report_summary.top_n, 3)
            self.assertFalse(shop.report_summary.show_average_per_day)

    def test_resolve_channel_id_uses_channel_name(self) -> None:
        shop = report.ShopConfig(
            name="Woodshop",
            captain_email="sender@example.invalid",
            doorflow_channel_name="Wood Shop Door",
        )
        config = report.AppConfig(
            api_base="https://api.doorflow.com/api/3",
            sendmail_path="/usr/sbin/sendmail",
            from_address="sender@example.invalid",
            default_email="sender@example.invalid",
            shops=(shop,),
        )
        with patch.object(
            report,
            "list_channels",
            return_value=[
                {"id": 4622, "name": "Wood Shop Door"},
                {"id": 4695, "name": "ChopShop"},
            ],
        ):
            self.assertEqual(report.resolve_channel_id(config, shop, "token"), 4622)

    def test_collect_badge_events_orders_oldest_first(self) -> None:
        events = [
            {
                "person_name": "Ada Lovelace",
                "credentials_number": "1234",
                "created_at": "2026-07-10T15:30:00Z",
                "event_code": 10,
                "event_label": "Ada admitted using card",
            },
            {
                "person_name": "Grace Hopper",
                "credentials_number": "9876",
                "created_at": "2026-07-01T08:45:00Z",
                "event_code": 20,
                "event_label": "Grace rejected using card",
            },
        ]

        collected = report.collect_badge_events(events)
        self.assertEqual([event.person_name for event in collected], ["Grace Hopper", "Ada Lovelace"])
        self.assertEqual([event.status for event in collected], ["Rejected", "Accepted"])
        self.assertEqual(collected[0].created_at, datetime(2026, 7, 1, 8, 45, tzinfo=timezone.utc))
        self.assertEqual(collected[1].created_at, datetime(2026, 7, 10, 15, 30, tzinfo=timezone.utc))

    def test_send_via_sendmail_uses_envelope_sender(self) -> None:
        message = EmailMessage()
        message["Subject"] = "test"
        message.set_content("hello")

        with patch.object(report.subprocess, "run") as run:
            report.send_via_sendmail(message, "/usr/sbin/sendmail", "reports@example.invalid")

        run.assert_called_once()
        args, kwargs = run.call_args
        self.assertEqual(args[0][:4], ["/usr/sbin/sendmail", "-f", "reports@example.invalid", "-t"])
        self.assertTrue(kwargs["check"])
        self.assertIn("hello", kwargs["input"].decode())

    def test_build_summary_lines_includes_metrics_and_top_lists(self) -> None:
        events = [
            report.BadgeEvent(
                created_at=datetime(2026, 7, 1, 8, 45, tzinfo=timezone.utc),
                person_name="Ada Lovelace",
                credentials_number="1234",
                status="Accepted",
            ),
            report.BadgeEvent(
                created_at=datetime(2026, 7, 1, 9, 15, tzinfo=timezone.utc),
                person_name="Grace Hopper",
                credentials_number="9876",
                status="Rejected",
            ),
            report.BadgeEvent(
                created_at=datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc),
                person_name="Ada Lovelace",
                credentials_number="1234",
                status="Accepted",
            ),
        ]

        lines = report.build_summary_lines(
            events=events,
            period_days=2,
            summary_config=report.ReportSummaryConfig(),
        )

        self.assertEqual(lines[0], "Summary:")
        self.assertIn("  Total badge events: 3", lines)
        self.assertIn("  Accepted: 2", lines)
        self.assertIn("  Rejected: 1", lines)
        self.assertIn("  Unique badge holders: 2", lines)
        self.assertIn("  Average badges per day: 1.5", lines)
        self.assertIn("  Busiest day: 2026-07-01 (2 events)", lines)
        self.assertIn("Top 5 accepted badge holders:", lines)
        self.assertIn("  - Ada Lovelace (2)", lines)
        self.assertIn("Top 5 rejected attempts:", lines)
        self.assertIn("  - Grace Hopper (1)", lines)

    def test_build_summary_lines_can_be_disabled(self) -> None:
        events = [
            report.BadgeEvent(
                created_at=datetime(2026, 7, 1, 8, 45, tzinfo=timezone.utc),
                person_name="Ada Lovelace",
                credentials_number="1234",
                status="Accepted",
            )
        ]
        lines = report.build_summary_lines(
            events=events,
            period_days=2,
            summary_config=report.ReportSummaryConfig(enabled=False),
        )
        self.assertEqual(lines, [])

    def test_build_email_contains_body_and_csv_attachment(self) -> None:
        shop = report.ShopConfig(
            name="Woodshop",
            captain_email="sender@example.invalid",
            doorflow_channel_name="Wood Shop Door",
        )
        events = [
            report.BadgeEvent(
                created_at=datetime(2026, 7, 1, 8, 45, tzinfo=timezone.utc),
                person_name="Grace Hopper",
                credentials_number="9876",
                status="Rejected",
            ),
            report.BadgeEvent(
                created_at=datetime(2026, 7, 10, 15, 30, tzinfo=timezone.utc),
                person_name="Ada Lovelace",
                credentials_number="1234",
                status="Accepted",
            ),
        ]
        message = report.build_email(
            subject="Woodshop badge report",
            sender="sender@example.invalid",
            recipients=["sender@example.invalid", "captain@example.invalid"],
            shop=shop,
            period_label="Last 30 Days",
            events=events,
            period_days=30,
        )

        self.assertIsInstance(message, EmailMessage)
        self.assertEqual(message["Subject"], "Woodshop badge report")
        self.assertEqual(message["To"], "sender@example.invalid, captain@example.invalid")
        body = message.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("Doorflow badge report for Woodshop", body)
        self.assertIn("Summary:", body)
        self.assertNotIn("Recipient:", body)
        self.assertIn("Accepted/Rejected", body)
        self.assertIn("2026-07-01 04:45:00 EDT | Rejected | Grace Hopper | 9876", body)
        attachment_text = message.get_payload()[1].get_content()
        self.assertIn("date_time_eastern,accepted_rejected,person_name,fob_number", attachment_text)
        self.assertIn("Grace Hopper", attachment_text)
        self.assertIn("Rejected", attachment_text)


if __name__ == "__main__":
    unittest.main()
