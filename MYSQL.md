# MySQL setup

The app stores data in MySQL by default (`components/database.py`, schema in `mysql/schema.sql`).

## 1. Create a database and user
Run in a MySQL shell as root (choose your own password):

```sql
CREATE DATABASE tgm_assets CHARACTER SET utf8mb4;
CREATE USER 'tgm'@'localhost' IDENTIFIED BY 'YOUR_PASSWORD';
GRANT ALL ON tgm_assets.* TO 'tgm'@'localhost';
```

## 2. Environment variables
```powershell
$env:MYSQL_HOST = "127.0.0.1"   # default
$env:MYSQL_PORT = "3306"        # default
$env:MYSQL_USER = "tgm"
$env:MYSQL_PASSWORD = "YOUR_PASSWORD"
$env:MYSQL_DATABASE = "tgm_assets"
$env:SECRET_KEY = "a-long-random-string"   # for hosting
```

## 3. Import existing CSV data (tables are created automatically)
```powershell
.\.venv\Scripts\python.exe scripts\import_csv_to_mysql.py --dry-run
.\.venv\Scripts\python.exe scripts\import_csv_to_mysql.py
```
Existing passwords keep working.

## 4. Run
```powershell
.\.venv\Scripts\python.exe app.py
```
Set `TGM_DATABASE_BACKEND=csv` to use the local CSV files instead (testing only).

Note: the logo and `smtp.json` are local files; on hosts with ephemeral disks use `SMTP_*` env vars.
