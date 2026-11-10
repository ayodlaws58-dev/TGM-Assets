# MySQL database

The app uses MySQL by default. Accounts (including existing password hashes), assets,
work orders, procurement requests and notifications are stored in MySQL. The CSV
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

The import creates tables in the configured database and imports all five CSVs in
one transaction. It refuses to import if any destination table already contains
records; it does not overwrite existing MySQL data. The database and user must
already exist. The app does not import automatically at startup.

For isolated tests or a temporary return to CSV, set `TGM_DATABASE_BACKEND=csv`.
Switching back does not copy MySQL changes into CSV.

The app replaces whole tables on writes, so run **one application process only**,
with no external writers or concurrent import. In particular use one Gunicorn
worker. Multiple processes can otherwise overwrite each other's changes. The
logo, SMTP configuration and generated secret key remain local files. For hosted
deployments, configure `SECRET_KEY` and either `RESEND_API_KEY` plus
`RESEND_FROM_EMAIL`, or the `SMTP_*` environment variables. Resend takes
precedence over SMTP when `RESEND_API_KEY` is set; verify the sender domain with
Resend before sending.
