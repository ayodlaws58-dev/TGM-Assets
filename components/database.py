import os
from contextlib import contextmanager

import pandas as pd
import pymysql
from dotenv import load_dotenv

from components import store

load_dotenv(store.DATA.parent / ".env")

TABLES = {
    "users": ("username", ("username", "full_name", "role", "salt", "hash", "email",
                           "reset_hash", "reset_expires", "reset_attempts")),
    "assets": ("asset_id", tuple(store.ASSET_COLS)),
    "work_orders": ("wo_id", tuple(store.WO_COLS)),
    "procurement": ("req_id", tuple(store.PR_COLS)),
    "notifications": ("notif_id", ("notif_id", "username", "kind", "title", "message",
                                   "created", "read", "key")),
}


def use_csv_backend():
    backend = os.environ.get("TGM_DATABASE_BACKEND", "mysql").strip().lower()
    if backend not in ("mysql", "csv"):
        raise ValueError("TGM_DATABASE_BACKEND must be 'mysql' or 'csv'.")
    return backend == "csv"


def connect():
    user = os.environ.get("MYSQL_USER", "").strip()
    name = os.environ.get("MYSQL_DATABASE", "").strip()
    if not user or not name or "MYSQL_PASSWORD" not in os.environ:
        raise RuntimeError("Set MYSQL_USER, MYSQL_PASSWORD and MYSQL_DATABASE before using MySQL.")
    return pymysql.connect(
        host=os.environ.get("MYSQL_HOST", "localhost"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=user, password=os.environ["MYSQL_PASSWORD"], database=name,
        charset="utf8mb4", autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )


@contextmanager
def transaction():
    connection = connect()
    try:
        with connection.cursor() as cursor:
            yield cursor
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _columns(table, columns):
    if table not in TABLES or tuple(columns) != TABLES[table][1]:
        raise ValueError("Unknown table or mismatched columns.")
    return ", ".join(f"`{column}`" for column in columns)


def initialize(cursor):
    for table, (key, columns) in TABLES.items():
        definitions = [f"`{key}` VARCHAR(191) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL PRIMARY KEY"]
        definitions.extend(f"`{column}` LONGTEXT NOT NULL" for column in columns if column != key)
        cursor.execute(f"CREATE TABLE IF NOT EXISTS `{table}` "
                       f"(`seq` BIGINT NOT NULL AUTO_INCREMENT UNIQUE, {', '.join(definitions)}) "
                       "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")


def dataframe(table, columns):
    cols = _columns(table, columns)
    with transaction() as cursor:
        cursor.execute(f"SELECT {cols} FROM `{table}` ORDER BY `seq`")
        rows = cursor.fetchall()
    return pd.DataFrame(rows, columns=columns).fillna("").astype(str)


def insert_rows(cursor, table, df, columns):
    cols = _columns(table, columns)
    rows = list(df.reindex(columns=columns).fillna("").astype(str).itertuples(index=False, name=None))
    if rows:
        placeholders = ", ".join(["%s"] * len(columns))
        cursor.executemany(f"INSERT INTO `{table}` ({cols}) VALUES ({placeholders})", rows)


def save_table(table, df, columns):
    _columns(table, columns)
    with transaction() as cursor:
        cursor.execute(f"DELETE FROM `{table}`")
        insert_rows(cursor, table, df, columns)
