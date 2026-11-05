import os
from contextlib import contextmanager
from pathlib import Path

import pandas as pd
import pymysql
from pymysql.cursors import DictCursor

SCHEMA_FILE = Path(__file__).resolve().parent.parent / "mysql" / "schema.sql"

TABLE_KEYS = {
    "assets": "asset_id",
    "work_orders": "wo_id",
    "procurement": "req_id",
    "users": "username",
    "notifications": "notif_id",
}


def use_csv_backend():
    backend = os.environ.get("TGM_DATABASE_BACKEND", "mysql").strip().lower()
    if backend not in ("mysql", "csv"):
        raise ValueError("TGM_DATABASE_BACKEND must be 'mysql' or 'csv'.")
    return backend == "csv"


def _connect(with_database=True):
    name = os.environ.get("MYSQL_DATABASE", "").strip()
    user = os.environ.get("MYSQL_USER", "").strip()
    if not name or not user:
        raise RuntimeError("Set MYSQL_USER, MYSQL_PASSWORD and MYSQL_DATABASE (and optionally "
                           "MYSQL_HOST / MYSQL_PORT) to connect to MySQL.")
    return pymysql.connect(
        host=os.environ.get("MYSQL_HOST", "127.0.0.1"), port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=user, password=os.environ.get("MYSQL_PASSWORD", ""),
        database=name if with_database else None, charset="utf8mb4", cursorclass=DictCursor,
        autocommit=False)


@contextmanager
def _transaction():
    conn = _connect()
    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_schema():
    """Create the database (if allowed) and all tables from mysql/schema.sql."""
    name = os.environ.get("MYSQL_DATABASE", "").strip()
    conn = _connect(with_database=False)
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{name}` CHARACTER SET utf8mb4")
            cur.execute(f"USE `{name}`")
            statements = [s.strip() for s in SCHEMA_FILE.read_text(encoding="utf-8").split(";") if s.strip()]
            for statement in statements:
                cur.execute(statement)
        conn.commit()
    finally:
        conn.close()


def _check(table):
    if table not in TABLE_KEYS:
        raise ValueError(f"Unknown table: {table}")
    return TABLE_KEYS[table]


def dataframe(table, columns):
    _check(table)
    cols = ", ".join(f"`{c}`" for c in columns)
    with _transaction() as cur:
        cur.execute(f"SELECT {cols} FROM `{table}` ORDER BY `seq`")
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=columns).fillna("").astype(str)


def _records(df, columns):
    return df.reindex(columns=columns).fillna("").astype(str).to_dict("records")


def _upsert(cur, table, key, records, columns):
    if not records:
        return
    cols = ", ".join(f"`{c}`" for c in columns)
    marks = ", ".join(["%s"] * len(columns))
    updates = ", ".join(f"`{c}`=VALUES(`{c}`)" for c in columns if c != key)
    cur.executemany(
        f"INSERT INTO `{table}` ({cols}) VALUES ({marks}) ON DUPLICATE KEY UPDATE {updates}",
        [[r[c] for c in columns] for r in records])


def upsert_table(table, df, columns):
    key = _check(table)
    with _transaction() as cur:
        _upsert(cur, table, key, _records(df, columns), columns)


def save_table(table, df, columns):
    """Make the table match `df`: insert/update its rows and delete rows that are no longer present."""
    key = _check(table)
    records = _records(df, columns)
    keep = [r[key] for r in records]
    with _transaction() as cur:
        _upsert(cur, table, key, records, columns)
        if keep:
            marks = ", ".join(["%s"] * len(keep))
            cur.execute(f"DELETE FROM `{table}` WHERE `{key}` NOT IN ({marks})", keep)
        else:
            cur.execute(f"DELETE FROM `{table}`")
