# Doorflow Badge Report

Self-contained reporting for DoorFlow door access events.

Requirements:
- Python 3.10+ (run with `python3`)
- A DoorFlow OAuth access token in the environment

The repo is intentionally config-driven:
- `config.json` holds the API base URL, sendmail path, sender address, shop/door definitions, per-shop summary toggles, the per-shop report interval, and the default copy recipient
- `badge_report.py` contains the report logic only
- `DOORFLOW_ACCESS_TOKEN` is the preferred environment variable for live API access

Configuration reference:

Top-level keys:
- `api_base`: DoorFlow API base URL, usually `https://api.doorflow.com/api/3`
- `sendmail_path`: path to local `sendmail`
- `from_address`: sender address used in the email header and envelope sender
- `default_email`: extra copy recipient that gets every report
- `state_path`: optional path to the JSON file that stores the last successful send times; if omitted, it defaults to `badge_report_state.json` next to `config.json`
- `shops`: array of shop objects

Per-shop keys:
- `name`: friendly shop name used on the command line and in the email subject
- `captain_email`: shop captain recipient
- `doorflow_channel_name`: DoorFlow channel name to look up, if you do not want to hardcode an ID
- `doorflow_channel_id`: DoorFlow channel ID, if you want to skip the lookup
- `report_every_days`: how often to send this shop’s report; defaults to 30
- `summary`: optional block controlling the summary section

Summary keys:
- `enabled`: turn the summary block on or off
- `show_total`: include total event count
- `show_status_counts`: include accepted/rejected counts
- `show_unique_people`: include unique badge holder count
- `show_average_per_day`: include average badges per day
- `show_busiest_day`: include busiest day
- `show_top_accepted_people`: include top accepted badge holders
- `show_top_rejected_people`: include top rejected attempts
- `top_n`: how many names to show in each top list

Example `config.json`:

```json
{
  "api_base": "https://api.doorflow.com/api/3",
  "sendmail_path": "/usr/sbin/sendmail",
  "from_address": "sender@example.invalid",
  "default_email": "sender@example.invalid",
  "state_path": "badge_report_state.json",
  "shops": [
    {
      "name": "Woodshop",
      "captain_email": "captain@example.invalid",
      "doorflow_channel_name": "Wood Shop Door",
      "report_every_days": 30,
      "summary": {
        "enabled": true,
        "top_n": 5
      }
    },
    {
      "name": "Metal Shop",
      "captain_email": "metalshop@example.invalid",
      "doorflow_channel_id": 4722,
      "report_every_days": 14,
      "summary": {
        "enabled": true,
        "show_top_rejected_people": false
      }
    }
  ]
}
```

Current test setup:
- Woodshop is the default shop in `config.json`
- reports go to the shop captain plus whatever address is set in `default_email`

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
- optional `summary` block to enable/disable sections per shop

Then run, for example:

    python3 badge_report.py --shop MetalShop

If you omit `--shop`, the script checks every configured shop and sends only the ones that are due.
If you omit `--days`, each shop uses its configured interval from `report_every_days` (default 30).
If you do pass `--days`, it overrides the interval for that run.

Manual trigger examples:

- Force one shop immediately:

      python3 badge_report.py --shop MetalShop --force

- Force every configured shop immediately:

      python3 badge_report.py --force

State:

- The script records last successful send times in `badge_report_state.json` by default.
- `state_path` in `config.json` can override that location.

Testing:

    python3 -m unittest discover -s tests -v
