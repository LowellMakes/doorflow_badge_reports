# Doorflow Badge Report

Self-contained reporting for DoorFlow door access events.

Requirements
- Python 3.10+ (run with `python3`)
- A DoorFlow OAuth access token in the environment
- Local `sendmail` access for delivery

What it does
- looks up the configured DoorFlow door for each shop
- fetches access events for the relevant window
- keeps events oldest-first
- shows Eastern time, person name, fob/credential number, and accepted/rejected status
- adds a compact summary at the top of the email
- emails a human-readable report plus CSV attachment via local sendmail

Configuration overview
The script is intentionally config-driven. `config.json` controls:
- the DoorFlow API base URL
- the local `sendmail` path
- the sender address
- the default copy recipient
- the persisted state file location
- per-shop door, captain, and interval settings
- per-shop summary toggles

The preferred environment variable for DoorFlow auth is:
- `DOORFLOW_ACCESS_TOKEN`

Configuration reference
Top-level keys:
- `api_base`: DoorFlow API base URL, usually `https://api.doorflow.com/api/3`
- `sendmail_path`: path to local `sendmail`
- `from_address`: sender address used in the email header and envelope sender
- `default_email`: extra copy recipient that gets every report
- `state_path`: optional path to the JSON file that stores last successful send times; if omitted, it defaults to `badge_report_state.json` next to `config.json`
- `shops`: array of shop objects

Per-shop keys:
- `name`: friendly shop name used on the command line and in the email subject
- `captain_email`: shop captain recipient
- `doorflow_channel_name`: DoorFlow channel name to look up
- `doorflow_channel_id`: DoorFlow controller/channel id to use instead of the name
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

Example config
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

How scheduling works
- The script records the last successful send time for each shop in the state file.
- By default, that file is `badge_report_state.json` next to `config.json`.
- Each shop’s `report_every_days` controls how often it is due.
- If a shop has never been sent, it uses its configured interval as the initial lookback window.
- On a normal run, the script checks every configured shop and sends only the ones that are due.
- If you pass `--shop`, it checks only that one shop.
- If you pass `--days`, it overrides the interval for that run.
- If you pass `--force`, the script sends even if the shop is not due yet.
- A successful send updates that shop’s last-sent timestamp.

CLI usage
Set your DoorFlow token:

```bash
export DOORFLOW_ACCESS_TOKEN='your-real-doorflow-access-token'
```

Dry-run the current config:

```bash
python3 badge_report.py --dry-run
```

Send due reports for every configured shop:

```bash
python3 badge_report.py
```

Force one shop immediately:

```bash
python3 badge_report.py --shop MetalShop --force
```

Force every configured shop immediately:

```bash
python3 badge_report.py --force
```

Useful notes
- The email goes to both the shop captain and `default_email`.
- If those addresses are the same, duplicates are removed.
- `--dry-run` still fetches DoorFlow data so the rendered report is real; it just prints the email instead of sending it.
- The CSV attachment uses the same event rows as the human-readable table.

### Quick start
1. Set `DOORFLOW_ACCESS_TOKEN`.
2. Review `config.json`.
3. Run `python3 badge_report.py --dry-run`.
4. Send with `python3 badge_report.py`.

Systemd scheduling
The repo includes these unit files under `systemd/`:
- `systemd/doorflow-badge-report.service`
- `systemd/doorflow-badge-report.timer`

They are written for a runtime checkout at `/opt/doorflow_badge_report` and a weekly midnight timer.
The script itself still decides which shops are due based on each shop’s `report_every_days` value.

A typical install looks like this:

```bash
sudo install -D -m 0644 systemd/doorflow-badge-report.service /etc/systemd/system/doorflow-badge-report.service
sudo install -D -m 0644 systemd/doorflow-badge-report.timer /etc/systemd/system/doorflow-badge-report.timer
sudo systemctl daemon-reload
sudo systemctl enable --now doorflow-badge-report.timer
```

The service runs as root, so it can be installed directly under `/etc/systemd/system/` and launched by the timer without a dedicated service account.
The service reads `DOORFLOW_ACCESS_TOKEN` from `/etc/default/doorflow-badge-report` if that file exists.
Create it with the token before enabling the timer:

```bash
sudo install -D -m 0600 /dev/null /etc/default/doorflow-badge-report
sudoedit /etc/default/doorflow-badge-report
```

Add:

```bash
DOORFLOW_ACCESS_TOKEN=your-real-doorflow-access-token
```

Testing
```bash
python3 -m unittest discover -s tests -v
```
