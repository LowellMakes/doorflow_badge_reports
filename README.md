# Doorflow Badge Report

A self-contained monthly report script for Doorflow membership badges by shop.

For now it ships with the Woodshop configuration and sends the report to sender@example.invalid for testing.

## What it does

- Fetches all Doorflow people from the API
- Filters the people assigned to the requested shop's Doorflow group
- Builds a human-readable report and CSV attachment
- Emails the report via local sendmail

## Quick start

1. Set your Doorflow auth key:

   export DOORFLOW_AUTH_KEY='your-real-doorflow-auth-key'

2. Review or edit `config.json`.

3. Dry-run the Woodshop report:

   python badge_report.py --dry-run

4. Send the report:

   python badge_report.py

## Customizing later

Add more entries to `config.json` under `shops` for each room/shop:

- name
- doorflow_group_id
- captain_email

Then run:

    python badge_report.py --shop MetalShop

## Testing

    python -m unittest discover -s tests -v
