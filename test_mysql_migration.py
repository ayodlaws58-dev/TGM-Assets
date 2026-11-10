from contextlib import contextmanager

import pandas as pd
import pytest

from components import auth, database, notify, store
from scripts import import_csv_to_mysql as importer


class Cursor:
    def __init__(self, counts=None, existing_columns=None):
        self.statements = []
        self.counts = counts or {}
        self.existing_columns = existing_columns
        self.table = None

    def execute(self, sql, *args):
        self.statements.append(sql)
        if sql.startswith("SELECT COUNT"):
            self.table = sql.split("`")[1]
        elif sql.startswith("SHOW COLUMNS"):
            self.table = sql.split("`")[1]

    def fetchone(self):
        return {"count": self.counts.get(self.table, 0)}

    def fetchall(self):
        if self.table in database.TABLES:
            columns = self.existing_columns
            if columns is None:
                columns = database.TABLES[self.table][1]
            return [{"Field": column} for column in columns]
        return []

    def executemany(self, sql, rows):
        self.statements.append((sql, rows))


def test_mysql_save_uses_validated_columns_and_parameterized_rows(monkeypatch):
    cursor = Cursor()
    monkeypatch.setattr(database, "SCHEMA_READY", set())

    @contextmanager
    def transaction():
        yield cursor

    monkeypatch.setattr(database, "transaction", transaction)
    row = {column: "" for column in store.ASSET_COLS}
    row.update(asset_id="AST-1", name="Generator")
    database.save_table("assets", pd.DataFrame([row]), store.ASSET_COLS)
    assert cursor.statements[2] == "DELETE FROM `assets`"
    statement, values = cursor.statements[3]
    assert "%s" in statement and "Generator" not in statement
    assert values[0][0:2] == ("AST-1", "Generator")
    with pytest.raises(ValueError):
        database.save_table("assets", pd.DataFrame([row]), ["asset_id"])


def test_mysql_asset_schema_migrates_existing_tables(monkeypatch):
    old_columns = ["asset_id", "name", "category", "location", "last_service",
                   "interval_days", "notes"]
    cursor = Cursor(existing_columns=old_columns)
    monkeypatch.setattr(database, "SCHEMA_READY", set())
    database._ensure_table_schema(cursor, "assets")
    alterations = [statement for statement in cursor.statements if statement.startswith("ALTER TABLE")]
    assert len(alterations) == len(store.ASSET_COLS) - len(old_columns)
    assert all("LONGTEXT NULL" in statement for statement in alterations)


def test_mysql_notification_schema_adds_email_delivery_tracking(monkeypatch):
    old_columns = ["notif_id", "username", "kind", "title", "message", "created", "read", "key"]
    cursor = Cursor(existing_columns=old_columns)
    monkeypatch.setattr(database, "SCHEMA_READY", set())
    database._ensure_table_schema(cursor, "notifications")
    assert "ALTER TABLE `notifications` ADD COLUMN `email_sent` LONGTEXT NULL" in cursor.statements


def test_transaction_rolls_back_on_insert_failure(monkeypatch):
    class Connection:
        committed = False
        rolled_back = False
        closed = False

        @contextmanager
        def cursor(self):
            yield Cursor()

        def commit(self):
            self.committed = True

        def rollback(self):
            self.rolled_back = True

        def close(self):
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(database, "connect", lambda: connection)
    with pytest.raises(ValueError):
        with database.transaction():
            raise ValueError("failed")
    assert connection.rolled_back and connection.closed and not connection.committed


def test_import_refuses_nonempty_destination(monkeypatch):
    frames = {table: pd.DataFrame(columns=columns)
              for table, (_, columns) in database.TABLES.items()}
    cursor = Cursor({"assets": 1})

    @contextmanager
    def transaction():
        yield cursor

    monkeypatch.setattr(importer, "read_csv_files", lambda: frames)
    monkeypatch.setattr(database, "transaction", transaction)
    monkeypatch.setattr("sys.argv", ["import_csv_to_mysql.py"])
    with pytest.raises(RuntimeError, match="assets is not empty"):
        importer.main()
    assert not any(isinstance(statement, tuple) for statement in cursor.statements)


def test_import_preserves_csv_password_hashes_and_all_tables(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    for table, (key, columns) in database.TABLES.items():
        row = dict.fromkeys(columns, "")
        row[key] = "test-id"
        if table == "users":
            row.update(salt="test-salt", hash="test-hash")
        if table == "assets":
            row["notes"] = "NA"
        pd.DataFrame([row], columns=columns).to_csv(data / f"{table}.csv", index=False)
    monkeypatch.setattr(importer, "ROOT", tmp_path)
    frames = importer.read_csv_files()
    assert frames["users"].iloc[0]["salt"] == "test-salt"
    assert frames["users"].iloc[0]["hash"] == "test-hash"
    assert frames["assets"].iloc[0]["notes"] == "NA"
    cursor = Cursor()

    @contextmanager
    def transaction():
        yield cursor

    monkeypatch.setattr(database, "transaction", transaction)
    monkeypatch.setattr("sys.argv", ["import_csv_to_mysql.py"])
    importer.main()
    inserts = [statement for statement in cursor.statements if isinstance(statement, tuple)]
    assert len(inserts) == len(database.TABLES)


def test_import_accepts_legacy_csv_without_new_maintenance_tables(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    for table, (key, columns) in database.TABLES.items():
        if table in ("maintenance_schedules", "service_history"):
            continue
        pd.DataFrame(columns=columns).to_csv(data / f"{table}.csv", index=False)
    monkeypatch.setattr(importer, "ROOT", tmp_path)
    frames = importer.read_csv_files()
    assert frames["maintenance_schedules"].empty
    assert frames["service_history"].empty


def test_mysql_backend_routes_all_storage_to_adapter(monkeypatch):
    monkeypatch.setenv("TGM_DATABASE_BACKEND", "mysql")
    calls = []

    def load(table, columns):
        calls.append(("load", table))
        return pd.DataFrame(columns=columns)

    def save(table, frame, columns):
        calls.append(("save", table))

    monkeypatch.setattr(database, "dataframe", load)
    monkeypatch.setattr(database, "save_table", save)
    for table, loader, saver in [
        ("users", auth.load_users, auth.save_users),
        ("assets", store.load_assets, store.save_assets),
        ("work_orders", store.load_work_orders, store.save_work_orders),
        ("procurement", store.load_procurement, store.save_procurement),
        ("maintenance_schedules", store.load_schedules, store.save_schedules),
        ("service_history", store.load_service_history, store.save_service_history),
        ("notifications", notify._load, notify._save),
    ]:
        saver(loader())
        assert ("load", table) in calls
        assert ("save", table) in calls
