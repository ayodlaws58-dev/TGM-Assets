from datetime import date

import pandas as pd

from components import auth, database, mailer, store

FILE = store.DATA / "notifications.csv"
COLS = ["notif_id", "username", "kind", "title", "message", "created", "read", "key"]
KIND_LABEL = {"reminder": "Reminder", "overdue": "Overdue", "approval": "Decision", "request": "Request",
              "payment": "Payment", "info": "Update"}


def _load():
    if not database.use_csv_backend():
        return database.dataframe("notifications", COLS).reindex(columns=COLS).fillna("")
    try:
        df = pd.read_csv(FILE, dtype=str).fillna("")
    except (FileNotFoundError, pd.errors.EmptyDataError):
        df = pd.DataFrame(columns=COLS)
    return df[COLS]


def _save(df):
    if database.use_csv_backend():
        df.to_csv(FILE, index=False)
    else:
        database.save_table("notifications", df, COLS)


def recipients(minimum, exclude=()):
    users = auth.load_users()
    ok = users[users["role"].map(lambda r: auth.RANK[r] >= auth.RANK[minimum])]
    return [u for u in ok["username"] if u not in exclude]


def send(usernames, kind, title, message, key=""):
    """Create a notification per user. If `key` is given, each user gets it only once."""
    df = _load()
    ids = pd.to_numeric(df["notif_id"].str.extract(r"(\d+)$")[0], errors="coerce").dropna()
    n = int(ids.max()) if len(ids) else 0
    rows = []
    for u in dict.fromkeys(usernames):
        if key and ((df["username"] == u) & (df["key"] == key)).any():
            continue
        n += 1
        rows.append({"notif_id": f"N-{n}", "username": u, "kind": kind, "title": title,
                     "message": message, "created": date.today().isoformat(), "read": "", "key": key})
    if rows:
        _save(pd.concat([df, pd.DataFrame(rows)], ignore_index=True))
        if mailer.configured():
            users = auth.load_users().set_index("username")["email"]
            for r in rows:
                to = users.get(r["username"], "")
                if to:
                    mailer.send_email(to, f"[TGM Assets] {title}", message)


def for_user(username):
    df = _load()
    return df[df["username"] == username].iloc[::-1]


def unread_count(username):
    df = _load()
    return int(((df["username"] == username) & (df["read"] == "")).sum())


def mark_read(username, ids=None):
    df = _load()
    mask = (df["username"] == username) & (df["read"] == "")
    if ids is not None:
        mask &= df["notif_id"].isin(ids)
    df.loc[mask, "read"] = "1"
    _save(df)


def clear_read(username):
    df = _load()
    _save(df[~((df["username"] == username) & (df["read"] == "1"))])


def run_reminders():
    """Generate reminders for due/overdue assets and overdue work orders (admin and above)."""
    admins = recipients("admin")
    sched = store.with_due_dates(store.load_assets())
    for _, r in sched[sched["status"].isin(["Overdue", "Due soon"])].iterrows():
        when = f"{r['next_service']:%Y-%m-%d}"
        if r["status"] == "Overdue":
            send(admins, "overdue", f"{r['kind']} overdue: {r['name']}",
                 f"{r['name']} ({r['location']}) was due {when}.", key=f"asset-over:{r['asset_id']}:{when}")
        else:
            send(admins, "reminder", f"{r['kind']} due soon: {r['name']}",
                 f"{r['name']} ({r['location']}) is due {when}.", key=f"asset-soon:{r['asset_id']}:{when}")
    orders = store.load_work_orders()
    today = date.today().isoformat()
    late = orders[(orders["approval_status"] == "Approved") & (orders["status"] != "Completed")
                  & (orders["due"] != "") & (orders["due"] < today)]
    users = set(auth.load_users()["username"])
    for _, o in late.iterrows():
        who = admins + ([o["assigned_to"]] if o["assigned_to"] in users else [])
        send(who, "overdue", f"Work order overdue: {o['wo_id']}",
             f"{o['title']} was due {o['due']}.", key=f"wo-over:{o['wo_id']}:{o['due']}")
