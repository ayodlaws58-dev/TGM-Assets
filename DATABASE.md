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
worker. Multiple processes can otherwise overwrite each other's changes. The
uploaded asset files, logo, SMTP configuration and generated secret key remain local files; configure
`SECRET_KEY` and `SMTP_*` environment variables for hosted deployments.

Asset depreciation uses the straight-line method: purchase cost less salvage value,
spread across the recorded useful life. Book value is capped at salvage value and
depreciation stops at the recorded retirement or disposal date.

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
