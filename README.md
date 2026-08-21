# Doorflow Badge Report

Self-contained reporting for DoorFlow door access events.

Requirements:
- Python 3.10+ (run with `python3`)
- A DoorFlow OAuth access token in the environment

The repo is intentionally config-driven:
- `config.json` holds the API base URL, sendmail path, sender address, and shop/door definitions
- `badge_report.py` contains the report logic only
- `DOORFLOW_ACCESS_TOKEN` is the preferred environment variable for live API access

Current test setup:
- Woodshop is the default shop in `config.json`
- the report is addressed to `sender@example.invalid`

What it does:
- looks up the configured door controller for the selected shop
- fetches DoorFlow access events for the last 30 days
- keeps the badge events oldest-first
- shows the badge time in Eastern time, the person’s name, the fob/credential number used, and whether the access was accepted or rejected
- emails a human-readable report plus CSV attachment via local sendmail

Quick start:

1. Set your DoorFlow token:

   export DOORFLOW_ACCESS_TOKEN='your-real-doorflow-access-token'

2. Review `config.json`.

3. Dry-run the default shop:

   python3 badge_report.py --dry-run

4. Send the report:

   python3 badge_report.py

Changing shops later:

Add more entries to `config.json` under `shops`:

- `name`
- `captain_email`
- `doorflow_channel_name` or `doorflow_channel_id`

Then run, for example:

    python3 badge_report.py --shop MetalShop

If you omit `--shop`, the first shop in `config.json` is used.

Testing:

    python3 -m unittest discover -s tests -v
