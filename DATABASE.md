# MySQL database

The app uses MySQL by default. Accounts (including existing password hashes), assets,
work orders, preventive schedules, service history, procurement requests and notifications are stored in MySQL. Asset records include
ownership, parent-child relationships, purchase and depreciation data, lifecycle status and
attachment metadata. Existing MySQL asset tables gain any missing asset columns on first use.
Photos and PDF attachments are stored in `data/asset_uploads/` on the application host, so
include that directory in backups and use persistent storage in hosted deployments. The CSV
files in `data/` are not updated after migration; keep a backup before switching.

## Set up a new database

Install MySQL 8 and sign in to the MySQL shell as an administrator. Create a new,
empty database and a dedicated user, replacing the password placeholder locally:

```sql
CREATE DATABASE tgm_assets CHARACTER SET utf8mb4;
CREATE USER 'tgm'@'localhost' IDENTIFIED BY 'YOUR_CHOSEN_PASSWORD';
GRANT ALL PRIVILEGES ON tgm_assets.* TO 'tgm'@'localhost';
```

In PowerShell, install dependencies, copy `.env.example` to `.env`, and edit
`.env` to provide the MySQL credentials. The file is git-ignored; never commit
it or share its password. Existing environment variables take precedence.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

Stop the app before importing so the CSV files do not change during migration.
Back up the CSV files and run:

```powershell
.\.venv\Scripts\python.exe scripts\import_csv_to_mysql.py --dry-run
.\.venv\Scripts\python.exe scripts\import_csv_to_mysql.py
.\.venv\Scripts\python.exe app.py
```

The import creates tables in the configured database and imports the five legacy CSVs,
plus the maintenance and service-history CSVs when present, in one transaction. It refuses to import if any destination table already contains
records; it does not overwrite existing MySQL data. The database and user must
already exist. The app does not import automatically at startup.

Existing MySQL work-order tables gain the new completion fields on first use.
Preventive-schedule and service-history tables are created automatically when first accessed.
Legacy CSV installations do not need to create schedule or history files in advance.

For isolated tests or a temporary return to CSV, set `TGM_DATABASE_BACKEND=csv`.
Switching back does not copy MySQL changes into CSV.

The app replaces whole tables on writes, so run **one application process only**,
with no external writers or concurrent import. In particular use one Gunicorn
worker. Multiple processes can otherwise overwrite each other's changes. The uploaded asset files, logo, SMTP configuration and generated secret key remain local
files. For hosted deployments, configure `SECRET_KEY` and either `RESEND_API_KEY` plus
`RESEND_FROM_EMAIL`, or the `SMTP_*` environment variables. Resend takes precedence
over SMTP when an API key is set; verify the sender domain with Resend before sending.

Asset depreciation uses the straight-line method: purchase cost less salvage value,
spread across the recorded useful life. Book value is capped at salvage value and
depreciation stops at the recorded retirement or disposal date.

## Hosting with Cloudflare Tunnel

The app, MySQL and the uploads run on one Ubuntu 22.04/24.04 server. Cloudflare Tunnel
publishes the site at `https://assets.tgmeducation.com`. The server opens no inbound ports:
`cloudflared` connects out to Cloudflare, Gunicorn listens on `127.0.0.1:8000`, and MySQL
listens on `127.0.0.1` only.

1. Copy the repository to `/opt/tgm-assets` on the server, then run:

   ```bash
   cd /opt/tgm-assets && sudo bash deploy/setup_server.sh
   ```

   This installs MySQL 8 and the app's Python dependencies. It also creates the `tgm_assets`
   database and its user, and writes `/opt/tgm-assets/.env` with generated passwords,
   `SECRET_KEY` and `TRUST_PROXY=1`. Finally it installs the `tgm-assets` service, the hourly
   reminder timer and the nightly backup timer, and installs `cloudflared`. Running it again
   keeps an existing `.env` and database.
2. Add `RESEND_API_KEY` to `.env`, then run `sudo systemctl restart tgm-assets`.
3. Import the CSV data once with the commands the script prints.
4. In the Cloudflare dashboard, add `tgmeducation.com` to Cloudflare if it isn't there yet.
   Go to **Zero Trust → Networks → Tunnels**, create a tunnel, and run the
   `sudo cloudflared service install <TOKEN>` command it shows on the server. Add a public
   hostname `assets` on `tgmeducation.com` with service `http://127.0.0.1:8000`.

`TRUST_PROXY=1` makes the app read the visitor's address and `https` from the headers that
`cloudflared` adds, which sign-in lockouts and QR code links depend on. It also marks session
cookies secure. Set it only while Gunicorn listens on `127.0.0.1`.

Useful commands:

```bash
sudo systemctl status tgm-assets cloudflared   # is it running?
sudo journalctl -u tgm-assets -f               # app log
sudo systemctl start tgm-backup                # back up now
cd /opt/tgm-assets && sudo -u tgm git pull && sudo systemctl restart tgm-assets   # update
```

Backups go to `/var/backups/tgm-assets` every night at 02:30 and are kept for 14 days. Each
backup has a MySQL dump plus the uploads, logo and `.env`. Copy them off the server
regularly. To restore: `gunzip -c db_<date>.sql.gz | mysql tgm_assets`.

## Maintenance schedules and email reminders

Preventive schedules recur by calendar days, weeks, months or years. Usage-meter intervals
(such as generator hours) are not supported. Service completions update their linked schedule
and append to the asset's service history, including actual cost, completion notes and downtime.

For Resend delivery, set `RESEND_API_KEY` and `RESEND_FROM_EMAIL` in the app environment.
Use a sender address on a domain verified with Resend. When both values are set, Resend is used;
otherwise configured SMTP remains available as a fallback.
Configure a recurring system task to run the reminders script at the desired cadence (for
example, hourly):

```powershell
.\.venv\Scripts\python.exe scripts\run_reminders.py
```

The app also checks reminders while users visit pages. Scheduled execution ensures alerts
are sent even when the app has no active visitors. Reminder keys prevent repeat email for
the same due date.

## Seasonal themes

The site automatically displays a seasonal theme for the seven days before and the day
of Easter Sunday, Nigerian Independence Day (October 1), Halloween (October 31),
US Thanksgiving (the fourth Thursday in November), and Christmas (December 25).
Easter is calculated using the Gregorian calendar. Dates use the application server's
local date; outside these periods the standard theme is shown. Each theme also displays
a seasonal decoration in the sidebar and page headers, or above the sign-in form.
