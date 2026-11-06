import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from components import database


def read_csv_files():
    tables = {}
    for table, (key, columns) in database.TABLES.items():
        path = ROOT / "data" / f"{table}.csv"
        if not path.is_file():
            raise FileNotFoundError(f"Required CSV file not found: {path}")
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
        if key not in frame.columns:
            raise ValueError(f"{path.name} is missing the {key} column.")
        frame = frame.reindex(columns=columns, fill_value="")
        if (frame[key].str.strip() == "").any() or frame[key].duplicated().any():
            raise ValueError(f"{path.name} contains empty or duplicate {key} values.")
        tables[table] = frame
    return tables


def main():
    parser = argparse.ArgumentParser(description="Import CSV data into an empty MySQL database.")
    parser.add_argument("--dry-run", action="store_true", help="Validate and count CSV rows without connecting.")
    args = parser.parse_args()
    tables = read_csv_files()
    for table, frame in tables.items():
        print(f"{table}: {len(frame)} records")
    if args.dry_run:
        return
    with database.transaction() as cursor:
        database.initialize(cursor)
        for table in database.TABLES:
            cursor.execute(f"SELECT COUNT(*) AS count FROM `{table}`")
            if cursor.fetchone()["count"]:
                raise RuntimeError(f"MySQL table {table} is not empty. Import stopped without replacing data.")
        for table, frame in tables.items():
            database.insert_rows(cursor, table, frame, database.TABLES[table][1])
    print("Import complete. Original CSV files have not been changed.")


if __name__ == "__main__":
    main()
