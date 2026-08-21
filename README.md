# Doorflow Badge Report

Self-contained monthly reporting for Doorflow shop badges.

Requirements:
- Python 3.10+ (run with `python3`, not `python` on older systems)
- `DOORFLOW_AUTH_KEY` in the environment at runtime
- `config.json` holds API base, sendmail path, sender address, and shop definitions
- `badge_report.py` contains the logic only
- `DOORFLOW_AUTH_KEY` supplies the live Doorflow auth token at runtime

## Current test setup

The bundled config starts with Woodshop and sends the test report to `sender@example.invalid`.

## What it does

- Fetches all Doorflow people from the API
- Filters the people assigned to the selected shop’s Doorflow group
- Builds a human-readable report and CSV attachment
- Emails the report through local sendmail

## Quick start

1. Set your Doorflow auth key:

   export DOORFLOW_AUTH_KEY='your-real-doorflow-auth-key'

2. Review `config.json`.

3. Dry-run the default shop:

   python3 badge_report.py --dry-run

4. Send the report:

   python3 badge_report.py

## Changing shops later

Add more entries to `config.json` under `shops`:

- `name`
- `doorflow_group_id`
- `captain_email`

Then run, for example:

    python badge_report.py --shop MetalShop

If you omit `--shop`, the first shop in `config.json` is used.

## Testing

    python -m unittest discover -s tests -v
