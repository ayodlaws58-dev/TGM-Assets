import logging
from datetime import date

import pandas as pd

from components import auth, database, mailer, store

FILE = store.DATA / "notifications.csv"
COLS = ["notif_id", "username", "kind", "title", "message", "created", "read", "key", "email_sent"]
KIND_LABEL = {"reminder": "Reminder", "overdue": "Overdue", "approval": "Decision", "request": "Request",
              "payment": "Payment", "info": "Update"}
LOGGER = logging.getLogger(__name__)


def _load():
    if not database.use_csv_backend():
        return database.dataframe("notifications", COLS)
    try:
        df = pd.read_csv(FILE, dtype=str).fillna("")
    except (FileNotFoundError, pd.errors.EmptyDataError):
        df = pd.DataFrame(columns=COLS)
    for column in COLS:
        if column not in df:
            df[column] = ""
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


def send(usernames, kind, title, message, key="", template=None, variables=None):
    """Create a notification per user. If `key` is given, each user gets it only once.

    With `template` (a mailer.TEMPLATES key or a Resend template ID/alias), the email is sent from that
    Resend template filled with `variables`; otherwise the message text is emailed as before.
    """
    df = _load()
    ids = pd.to_numeric(df["notif_id"].str.extract(r"(\d+)$")[0], errors="coerce").dropna()
    n = int(ids.max()) if len(ids) else 0
    rows = []
    email_rows = []
    for u in dict.fromkeys(usernames):
        existing = df[(df["username"] == u) & (df["key"] == key)] if key else df.iloc[0:0]
        if not existing.empty:
            if existing.iloc[-1]["email_sent"] != "1":
                email_rows.append(existing.iloc[-1].to_dict())
            continue
        n += 1
        row = {"notif_id": f"N-{n}", "username": u, "kind": kind, "title": title,
               "message": message, "created": date.today().isoformat(), "read": "",
               "key": key, "email_sent": ""}
        rows.append(row)
        email_rows.append(row)
    if rows:
        _save(pd.concat([df, pd.DataFrame(rows)], ignore_index=True))
        df = pd.concat([df, pd.DataFrame(rows)], ignore_index=True)
    if email_rows and mailer.configured():
        users = auth.load_users().set_index("username")["email"]
        delivered = []
        for r in email_rows:
            to = users.get(r["username"], "")
            if not to:
                continue
            subject = f"[TGM Assets] {r['title']}"
            if template:
                error = mailer.send_template(to, template, variables, subject)
            else:
                error = mailer.send_email(to, subject, r["message"])
            if error:
                LOGGER.error("Could not email notification %s to %s: %s", r["notif_id"], r["username"], error)
            else:
                delivered.append(r["notif_id"])
        if delivered:
            df.loc[df["notif_id"].isin(delivered), "email_sent"] = "1"
            _save(df)


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
    """Generate calendar maintenance and overdue work-order reminders."""
    admins = recipients("admin")
    today_text = date.today().isoformat()
    sched = store.with_due_dates(store.load_assets())
    for _, r in sched[sched["status"].isin(["Overdue", "Due soon"])].iterrows():
        when = f"{r['next_service']:%Y-%m-%d}"
        if r["status"] == "Overdue":
            send(admins, "overdue", f"{r['kind']} overdue: {r['name']}",
                 f"{r['name']} ({r['location']}) was due {when}.",
                 key=f"asset-over:{r['asset_id']}:{when}:{today_text}")
        else:
            send(admins, "reminder", f"{r['kind']} due soon: {r['name']}",
                 f"{r['name']} ({r['location']}) is due {when}.", key=f"asset-soon:{r['asset_id']}:{when}")
    assets = store.load_assets()
    schedules = store.schedule_rows(store.load_schedules(), assets)
    users = set(auth.load_users()["username"])
    for row in schedules:
        if row["status"] not in ("Overdue", "Due soon"):
            continue
        when = row["next_due"].strftime("%Y-%m-%d")
        kind = "overdue" if row["status"] == "Overdue" else "reminder"
        who = admins + ([row["assigned_to"]] if row["assigned_to"] in users else [])
        send(who, kind, f"Preventive maintenance {row['status'].lower()}: {row['schedule_title']}",
             f"{row['asset_id']} {row['name']} ({row['location']}) is due {when}.",
             key=f"schedule-{row['status'].lower()}:{row['schedule_id']}:{when}"
             + (f":{today_text}" if row["status"] == "Overdue" else ""))
    orders = store.load_work_orders()
    late = orders[(orders["approval_status"] == "Approved") & (orders["status"] != "Completed")
                  & (orders["due"] != "") & (orders["due"] < today_text)]
    for _, o in late.iterrows():
        who = admins + ([o["assigned_to"]] if o["assigned_to"] in users else [])
        send(who, "overdue", f"Work order overdue: {o['wo_id']}",
             f"{o['title']} was due {o['due']}.",
             key=f"wo-over:{o['wo_id']}:{o['due']}:{today_text}")
