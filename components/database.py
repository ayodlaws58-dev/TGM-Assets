import os
from functools import lru_cache

import pandas as pd
from supabase import create_client


TABLE_KEYS = {
    "assets": "asset_id",
    "work_orders": "wo_id",
    "procurement": "req_id",
    "user_profiles": "username",
    "notifications": "notif_id",
}


def use_csv_backend():
    backend = os.environ.get("TGM_DATABASE_BACKEND", "supabase").strip().lower()
    if backend not in ("supabase", "csv"):
        raise ValueError("TGM_DATABASE_BACKEND must be 'supabase' or 'csv'.")
    return backend == "csv"


def _settings():
    url = os.environ.get("SUPABASE_URL", "").strip()
    anon_key = os.environ.get("SUPABASE_ANON_KEY", "").strip()
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    missing = [name for name, value in (
        ("SUPABASE_URL", url),
        ("SUPABASE_ANON_KEY", anon_key),
        ("SUPABASE_SERVICE_ROLE_KEY", service_key),
    ) if not value]
    if missing:
        raise RuntimeError("Missing required Supabase environment variables: " + ", ".join(missing))
    return url, anon_key, service_key


def auth_client():
    url, anon_key, service_key = _settings()
    return create_client(url, anon_key)


@lru_cache(maxsize=1)
def _admin_client(url, service_key):
    return create_client(url, service_key)


def admin_client():
    url, _, service_key = _settings()
    return _admin_client(url, service_key)


def load_rows(table):
    if table not in TABLE_KEYS:
        raise ValueError(f"Unknown Supabase table: {table}")
    rows = []
    start = 0
    while True:
        batch = admin_client().table(table).select("*").range(start, start + 999).execute().data
        rows.extend(batch)
        if len(batch) < 1000:
            return rows
        start += 1000


def upsert_rows(table, records):
    if table not in TABLE_KEYS:
        raise ValueError(f"Unknown Supabase table: {table}")
    if records:
        admin_client().table(table).upsert(records, on_conflict=TABLE_KEYS[table]).execute()


def save_table(table, df, columns):
    key = TABLE_KEYS.get(table)
    if not key:
        raise ValueError(f"Unknown Supabase table: {table}")
    records = df.reindex(columns=columns).fillna("").astype(str).to_dict("records")
    existing = load_rows(table)
    keep = {record[key] for record in records if record.get(key)}
    remove = [row[key] for row in existing if row.get(key) and row[key] not in keep]
    upsert_rows(table, records)
    for offset in range(0, len(remove), 100):
        admin_client().table(table).delete().in_(key, remove[offset:offset + 100]).execute()


def dataframe(table, columns):
    return pd.DataFrame(load_rows(table), columns=columns).fillna("")
