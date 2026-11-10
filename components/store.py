from pathlib import Path

import pandas as pd

DATA = Path(__file__).resolve().parent.parent / "data"

ASSET_COLS = ["asset_id", "name", "category", "location", "last_service", "interval_days", "notes",
              "parent_id", "owner", "purchase_date", "cost", "status", "useful_life_years",
              "salvage_value", "retired_on", "disposed_on", "disposal_value", "disposal_note",
              "attachments"]
WO_COLS = ["wo_id", "asset_id", "title", "description", "priority", "status",
           "assigned_to", "created", "due", "completed", "requested_by",
           "cost", "payment_status", "paid_on",
           "approval_status", "approved_by", "approved_on", "decision_note",
           "completion_notes", "actual_cost", "downtime_hours", "schedule_id"]
SCHEDULE_COLS = ["schedule_id", "asset_id", "title", "interval_value", "interval_unit",
                 "last_completed", "assigned_to", "description", "active", "created_by"]
SERVICE_COLS = ["service_id", "asset_id", "work_order_id", "schedule_id", "service_date",
                "summary", "completion_notes", "cost", "downtime_hours", "recorded_by", "created"]
PR_COLS = ["req_id", "item", "quantity", "unit_cost", "asset_id", "wo_id", "reason",
           "requested_by", "created", "status", "decided_by", "decided_on", "decision_note"]

CATEGORIES = ["AC", "Generator", "JMG Generator", "Building", "Painting",
              "Chairs", "Tables", "Other"]
PROCUREMENT_ITEMS = ["Diesel purchase", "Office stationery", "Generator spare parts", "AC spare parts",
                     "Paint and painting materials", "Furniture repair materials", "Cleaning supplies","Office-Furniture", "Other"]
REPAINT_CATEGORIES = ["Building", "Painting"]
ASSET_STATUSES = ["Active", "Under Maintenance", "Retired", "Disposed"]
SCHEDULE_UNITS = ["days", "weeks", "months", "years"]
STATUSES = ["New", "Assigned", "In Progress", "Completed"]
PRIORITIES = ["Low", "Medium", "High", "Urgent"]
PAYMENT_STATUSES = ["Unpaid", "Paid"]
APPROVAL_STATUSES = ["Pending", "Approved", "Rejected"]
PR_STATUSES = ["Pending", "Approved", "Rejected", "Ordered", "Received"]
PR_COMMITTED = ["Approved", "Ordered", "Received"]


def _load(name, cols):
    from components import database
    if not database.use_csv_backend():
        return database.dataframe(Path(name).stem, cols)
    try:
        df = pd.read_csv(DATA / name, dtype=str).fillna("")
    except (FileNotFoundError, pd.errors.EmptyDataError):
        df = pd.DataFrame(columns=cols)
    for c in cols:
        if c not in df:
            df[c] = ""
    return df[cols]


def load_assets():
    df = _load("assets.csv", ASSET_COLS)
    df.loc[df["status"] == "", "status"] = "Active"
    return df


def load_work_orders():
    df = _load("work_orders.csv", WO_COLS)
    df.loc[df["payment_status"] == "", "payment_status"] = "Unpaid"
    df.loc[df["approval_status"] == "", "approval_status"] = "Pending"
    return df


def load_procurement():
    return _load("procurement.csv", PR_COLS)


def load_schedules():
    return _load("maintenance_schedules.csv", SCHEDULE_COLS)


def load_service_history():
    return _load("service_history.csv", SERVICE_COLS)


def save_assets(df):
    _save("assets", df, ASSET_COLS)


def save_work_orders(df):
    _save("work_orders", df, WO_COLS)


def save_procurement(df):
    _save("procurement", df, PR_COLS)


def save_schedules(df):
    _save("maintenance_schedules", df, SCHEDULE_COLS)


def save_service_history(df):
    _save("service_history", df, SERVICE_COLS)


def _save(table, df, columns):
    from components import database
    if database.use_csv_backend():
        df.to_csv(DATA / f"{table}.csv", index=False)
    else:
        database.save_table(table, df, columns)


def add_row(df, **values):
    row = {c: "" for c in df.columns}
    row.update({k: str(v) for k, v in values.items()})
    return pd.concat([df, pd.DataFrame([row])], ignore_index=True)


def next_id(df, col, prefix, start):
    nums = pd.to_numeric(df[col].str.extract(r"(\d+)$")[0], errors="coerce").dropna()
    return f"{prefix}-{int(nums.max()) + 1 if len(nums) else start}"


def money(series):
    return pd.to_numeric(series, errors="coerce").fillna(0.0)


def pr_total(df):
    return money(df["quantity"]) * money(df["unit_cost"])


def add_calendar_interval(start, value, unit):
    """Add a calendar-based recurrence; no usage-meter intervals are supported."""
    start = pd.Timestamp(start)
    if unit == "days":
        return start + pd.DateOffset(days=value)
    if unit == "weeks":
        return start + pd.DateOffset(weeks=value)
    if unit == "months":
        return start + pd.DateOffset(months=value)
    if unit == "years":
        return start + pd.DateOffset(years=value)
    raise ValueError("Interval unit must be days, weeks, months or years.")


def schedule_rows(schedules, assets, today=None):
    today = pd.Timestamp(today or pd.Timestamp.today().normalize())
    asset_lookup = assets.set_index("asset_id").to_dict("index") if not assets.empty else {}
    rows = []
    for schedule in schedules.to_dict("records"):
        asset = asset_lookup.get(schedule["asset_id"])
        if not asset or schedule["active"] != "1" or asset["status"] in ("Retired", "Disposed"):
            continue
        try:
            value = int(schedule["interval_value"])
            last = pd.Timestamp(schedule["last_completed"])
            if pd.isna(last):
                continue
            due = add_calendar_interval(last, value, schedule["interval_unit"])
        except (TypeError, ValueError, OverflowError):
            continue
        rows.append({**schedule, **asset, "schedule_title": schedule["title"],
                     "next_due": due, "days_left": (due.normalize() - today).days,
                     "status": "Overdue" if due.normalize() < today else
                     "Due soon" if (due.normalize() - today).days <= 14 else "OK"})
    return sorted(rows, key=lambda row: row["next_due"])


def with_due_dates(assets, today=None):
    """Add next_service, days_left, status and kind (Servicing/Repainting) to assets."""
    today = pd.Timestamp(today or pd.Timestamp.today().normalize())
    df = assets.copy()
    lifecycle = df["status"].replace("", "Active") if "status" in df else pd.Series(index=df.index, dtype=str)
    last = pd.to_datetime(df["last_service"], errors="coerce")
    interval = pd.to_numeric(df["interval_days"], errors="coerce")
    df["next_service"] = last + pd.to_timedelta(interval, unit="D")
    df["days_left"] = (df["next_service"] - today).dt.days

    def label(d):
        if pd.isna(d):
            return "No schedule"
        if d < 0:
            return "Overdue"
        if d <= 14:
            return "Due soon"
        return "OK"

    df["status"] = df["days_left"].map(label)
    inactive = lifecycle.isin(["Retired", "Disposed"])
    df.loc[inactive, ["next_service", "days_left"]] = [pd.NaT, float("nan")]
    df.loc[inactive, "status"] = "No schedule"
    df["kind"] = df["category"].map(lambda c: "Repainting" if c in REPAINT_CATEGORIES else "Servicing")
    return df
