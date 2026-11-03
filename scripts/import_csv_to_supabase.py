import argparse
import os
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["TGM_DATABASE_BACKEND"] = "csv"

from components import auth, database, notify, store


def _user_id_by_email(client):
    found = {}
    page = 1
    while True:
        users = client.auth.admin.list_users(page=page, per_page=1000)
        found.update({user.email.lower(): str(user.id) for user in users if user.email})
        if len(users) < 1000:
            return found
        page += 1


def _users_to_profiles(csv_users, existing_auth_users, dry_run):
    existing_profiles = {} if dry_run else {
        row["username"]: row for row in database.load_rows("user_profiles")
    }
    profiles = []
    for row in csv_users.to_dict("records"):
        username = row["username"].strip().lower()
        email = row["email"].strip().lower()
        if not email:
            raise ValueError(f"Cannot import account '{username}': a valid email address is required.")
        if username in existing_profiles:
            profiles.append(existing_profiles[username])
            continue
        auth_uid = existing_auth_users.get(email)
        if not auth_uid and not dry_run:
            invited = database.admin_client().auth.admin.invite_user_by_email(
                email, options={"redirect_to": os.environ["SUPABASE_AUTH_REDIRECT_URL"]})
            auth_uid = str(invited.user.id)
            existing_auth_users[email] = auth_uid
        profiles.append({
            "username": username,
            "auth_uid": auth_uid or "",
            "full_name": row["full_name"],
            "role": row["role"],
            "email": email,
            "reset_salt": "",
            "reset_hash": "",
            "reset_expires": "",
            "reset_attempts": "",
        })
    return profiles


def main():
    parser = argparse.ArgumentParser(description="Import existing CSV records into Supabase.")
    parser.add_argument("--dry-run", action="store_true", help="Validate and report counts without writing or inviting users.")
    args = parser.parse_args()

    users = auth.load_users()
    invalid_email = users[users["email"].str.strip() == ""]
    if not invalid_email.empty:
        names = ", ".join(invalid_email["username"].tolist())
        raise ValueError(f"Add an email address for each account before importing: {names}")
    duplicate_emails = users["email"].str.strip().str.lower().duplicated(keep=False)
    if duplicate_emails.any():
        names = ", ".join(users.loc[duplicate_emails, "username"].tolist())
        raise ValueError(f"Resolve duplicate account email addresses before importing: {names}")

    if args.dry_run:
        existing_auth_users = {}
    else:
        if not os.environ.get("SUPABASE_AUTH_REDIRECT_URL", "").strip():
            raise ValueError("Set SUPABASE_AUTH_REDIRECT_URL to your app's /invite URL before importing users.")
        existing_auth_users = _user_id_by_email(database.admin_client())
    profiles = _users_to_profiles(users, existing_auth_users, args.dry_run)

    tables = [
        ("assets", store.ASSET_COLS, store.load_assets()),
        ("work_orders", store.WO_COLS, store.load_work_orders()),
        ("procurement", store.PR_COLS, store.load_procurement()),
        ("notifications", notify.COLS, notify._load()),
    ]
    print(f"{'Would import' if args.dry_run else 'Importing'} {len(profiles)} user profiles.")
    for table, columns, df in tables:
        print(f"{table}: {len(df)} records")
    if args.dry_run:
        return

    database.upsert_rows("user_profiles", profiles)
    for table, columns, df in tables:
        records = df.reindex(columns=columns).fillna("").astype(str).to_dict("records")
        database.upsert_rows(table, records)
    print("CSV import complete. Invited users should accept their Supabase Auth email invitation.")


if __name__ == "__main__":
    main()
