from pathlib import Path

import pandas as pd

DATA = Path(__file__).resolve().parent.parent / "data"

ASSET_COLS = ["asset_id", "name", "category", "location", "last_service", "interval_days", "notes"]
WO_COLS = ["wo_id", "asset_id", "title", "description", "priority", "status",
           "assigned_to", "created", "due", "completed", "requested_by",
           "cost", "payment_status", "paid_on",
           "approval_status", "approved_by", "approved_on", "decision_note"]
PR_COLS = ["req_id", "item", "quantity", "unit_cost", "asset_id", "wo_id", "reason",
           "requested_by", "created", "status", "decided_by", "decided_on", "decision_note"]

CATEGORIES = ["AC", "Generator", "JMG Generator", "Building", "Painting",
              "Chairs", "Tables", "Other"]
PROCUREMENT_ITEMS = ["Diesel purchase", "Office stationery", "Generator spare parts", "AC spare parts",
                     "Paint and painting materials", "Furniture repair materials", "Cleaning supplies","Office-Furniture", "Other"]
REPAINT_CATEGORIES = ["Building", "Painting"]
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
    return _load("assets.csv", ASSET_COLS)


def load_work_orders():
    df = _load("work_orders.csv", WO_COLS)
    df.loc[df["payment_status"] == "", "payment_status"] = "Unpaid"
    df.loc[df["approval_status"] == "", "approval_status"] = "Pending"
    return df


def load_procurement():
    return _load("procurement.csv", PR_COLS)


def save_assets(df):
    _save("assets", df, ASSET_COLS)


def save_work_orders(df):
    _save("work_orders", df, WO_COLS)


def save_procurement(df):
    _save("procurement", df, PR_COLS)


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


def with_due_dates(assets, today=None):
    """Add next_service, days_left, status and kind (Servicing/Repainting) to assets."""
    today = pd.Timestamp(today or pd.Timestamp.today().normalize())
    df = assets.copy()
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
    df["kind"] = df["category"].map(lambda c: "Repainting" if c in REPAINT_CATEGORIES else "Servicing")
    return df
