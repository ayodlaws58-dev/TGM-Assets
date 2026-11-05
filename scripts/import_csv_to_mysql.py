import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from components import auth, database, notify, store


def main():
    parser = argparse.ArgumentParser(description="Create the MySQL tables and import the existing CSV data.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be imported without touching MySQL.")
    args = parser.parse_args()

    # Read the CSV files, then write to MySQL.
    os.environ["TGM_DATABASE_BACKEND"] = "csv"
    tables = [
        ("users", auth.COLS, auth.load_users()),
        ("assets", store.ASSET_COLS, store.load_assets()),
        ("work_orders", store.WO_COLS, store.load_work_orders()),
        ("procurement", store.PR_COLS, store.load_procurement()),
        ("notifications", notify.COLS, notify._load()),
    ]
    for table, _, df in tables:
        print(f"{table}: {len(df)} records")
    if args.dry_run:
        return

    database.init_schema()
    for table, columns, df in tables:
        database.upsert_table(table, df, columns)
    print("Import complete. Existing passwords still work.")


if __name__ == "__main__":
    main()
