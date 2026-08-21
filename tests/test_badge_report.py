from __future__ import annotations

import sys
import unittest
from email.message import EmailMessage
from pathlib import Path
from tempfile import TemporaryDirectory

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
                  "api_base": "https://admin.doorflow.com/api/2",
                  "sendmail_path": "/usr/sbin/sendmail",
                  "from_address": "sender@example.invalid",
                  "shops": [
                    {"name": "Woodshop", "doorflow_group_id": "4622", "captain_email": "sender@example.invalid"}
                  ]
                }
                """,
                encoding="utf-8",
            )
            config = report.load_config(cfg)
            shop = report.find_shop(config, "woodshop")
            self.assertEqual(shop.doorflow_group_id, "4622")
            self.assertEqual(shop.captain_email, "sender@example.invalid")

    def test_filter_people_by_group_handles_group_dicts_and_ids(self) -> None:
        people = [
            {"first_name": "Ada", "last_name": "Lovelace", "groups": [{"id": 4622}, {"id": 4482}]},
            {"first_name": "Grace", "last_name": "Hopper", "groups": [{"id": 4695}]},
            {"first_name": "Linus", "last_name": "Torvalds", "group_ids": ["4622", "4482"]},
        ]

        filtered = report.filter_people_by_group(people, "4622")
        self.assertEqual([person["first_name"] for person in filtered], ["Ada", "Linus"])

    def test_build_email_contains_body_and_csv_attachment(self) -> None:
        shop = report.ShopConfig(name="Woodshop", doorflow_group_id="4622", captain_email="sender@example.invalid")
        message = report.build_email(
            subject="Woodshop badge report",
            sender="sender@example.invalid",
            recipient="sender@example.invalid",
            shop=shop,
            period_label="December 2026",
            people=[
                {
                    "first_name": "Ada",
                    "last_name": "Lovelace",
                    "email": "ada@example.com",
                    "credentials_number": "1234",
                    "key_fob_number": "",
                    "pin": "",
                    "enabled": True,
                }
            ],
        )

        self.assertIsInstance(message, EmailMessage)
        self.assertEqual(message["Subject"], "Woodshop badge report")
        self.assertEqual(message["To"], "sender@example.invalid")
        self.assertIn("Doorflow badge report for Woodshop", message.get_body(preferencelist=("plain",)).get_content())
        attachment_text = message.get_payload()[1].get_content()
        self.assertIn("ada@example.com", attachment_text)


if __name__ == "__main__":
    unittest.main()
