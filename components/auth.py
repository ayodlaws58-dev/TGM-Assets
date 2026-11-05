import hashlib
import hmac
import re
import secrets
import time
from pathlib import Path

import pandas as pd

from components import database

USERS_FILE = Path(__file__).resolve().parent.parent / "data" / "users.csv"
COLS = ["username", "full_name", "role", "salt", "hash", "email",
        "reset_hash", "reset_expires", "reset_attempts"]
ROLES = ["developer", "super_admin", "admin", "user"]
RANK = {"user": 1, "admin": 2, "super_admin": 3, "developer": 4}
ROLE_LABEL = {"developer": "Developer", "super_admin": "Super Admin", "admin": "Admin", "user": "User"}
ITERATIONS = 200_000
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
RESET_TTL = 15 * 60
MAX_RESET_ATTEMPTS = 5


def hash_password(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS).hex()


def load_users():
    if not database.use_csv_backend():
        return database.dataframe("users", COLS)
    try:
        df = pd.read_csv(USERS_FILE, dtype=str).fillna("")
    except (FileNotFoundError, pd.errors.EmptyDataError):
        df = pd.DataFrame(columns=COLS)
    for c in COLS:
        if c not in df:
            df[c] = ""
    return df[COLS]


def save_users(df):
    if database.use_csv_backend():
        df.to_csv(USERS_FILE, index=False)
    else:
        database.save_table("users", df, COLS)


def _email_taken(users, email, exclude=None):
    same = users["email"].str.lower() == email.lower()
    if exclude:
        same &= users["username"] != exclude
    return bool(email) and bool(same.any())


def add_user(username, full_name, role, password, email=""):
    users = load_users()
    username = username.strip().lower()
    email = email.strip().lower()
    if role not in ROLES:
        return "Invalid role."
    if not username or len(password) < 8:
        return "Username required and password must be at least 8 characters."
    if (users["username"] == username).any():
        return "That username already exists."
    if email and not EMAIL_RE.match(email):
        return "Enter a valid email address."
    if _email_taken(users, email):
        return "That email is already used by another account."
    salt = secrets.token_hex(16)
    row = {c: "" for c in COLS}
    row.update(username=username, full_name=full_name.strip(), role=role, salt=salt,
               hash=hash_password(password, salt), email=email)
    save_users(pd.concat([users, pd.DataFrame([row])], ignore_index=True))
    return None


def set_email(username, email):
    users = load_users()
    email = email.strip().lower()
    if email and not EMAIL_RE.match(email):
        return "Enter a valid email address."
    if _email_taken(users, email, exclude=username):
        return "That email is already used by another account."
    users.loc[users["username"] == username, "email"] = email
    save_users(users)
    return None


def set_password(username, password):
    if len(password) < 8:
        return "Password must be at least 8 characters."
    users = load_users()
    salt = secrets.token_hex(16)
    i = users["username"] == username
    users.loc[i, ["salt", "hash"]] = [salt, hash_password(password, salt)]
    users.loc[i, ["reset_hash", "reset_expires", "reset_attempts"]] = ["", "", ""]
    save_users(users)
    return None


def delete_user(username):
    users = load_users()
    save_users(users[users["username"] != username])
    return None


def _find(users, identifier):
    ident = identifier.strip().lower()
    if not ident:
        return None
    rows = users[(users["username"] == ident) | ((users["email"] != "") & (users["email"].str.lower() == ident))]
    return None if rows.empty else rows.index[0]


def request_reset(identifier):
    """Email a 6-digit reset code. Returns an error only for setup problems so the caller can show the
    same message whether or not the account exists."""
    from components import mailer
    if not mailer.configured():
        return "Password reset by email is not set up yet. Ask an admin to reset your password."
    users = load_users()
    idx = _find(users, identifier)
    if idx is None or not users.at[idx, "email"]:
        hash_password("x", secrets.token_hex(16))
        return None
    issued = float(users.at[idx, "reset_expires"] or 0) - RESET_TTL
    if time.time() - issued < 60:
        return None  # throttle: one code per minute
    code = f"{secrets.randbelow(10**6):06d}"
    users.at[idx, "reset_hash"] = hash_password(code, users.at[idx, "salt"])
    users.at[idx, "reset_expires"] = str(time.time() + RESET_TTL)
    users.at[idx, "reset_attempts"] = "0"
    save_users(users)
    mailer.send_email(
        users.at[idx, "email"], "Your TGM Assets password reset code",
        f"Hello {users.at[idx, 'full_name'] or users.at[idx, 'username']},\n\n"
        f"Your password reset code is: {code}\n\nIt expires in 15 minutes. "
        "If you did not request this, you can ignore this email.")
    return None


def reset_with_code(identifier, code, new_password):
    users = load_users()
    idx = _find(users, identifier)
    bad = "Invalid or expired code."
    if idx is None or not users.at[idx, "reset_hash"]:
        return bad
    if time.time() > float(users.at[idx, "reset_expires"] or 0) or \
            int(users.at[idx, "reset_attempts"] or 0) >= MAX_RESET_ATTEMPTS:
        return bad
    if not hmac.compare_digest(hash_password(code.strip(), users.at[idx, "salt"]),
                               users.at[idx, "reset_hash"]):
        users.at[idx, "reset_attempts"] = str(int(users.at[idx, "reset_attempts"] or 0) + 1)
        save_users(users)
        return bad
    return set_password(users.at[idx, "username"], new_password)


def verify(username, password):
    users = load_users()
    row = users[users["username"] == username.strip().lower()]
    if row.empty:
        hash_password(password, secrets.token_hex(16))  # keep timing similar
        return None
    r = row.iloc[0]
    if hmac.compare_digest(hash_password(password, r["salt"]), r["hash"]):
        return r
    return None


def manageable_roles(role):
    """Roles a user of `role` may create or assign: strictly below their own (developers may add developers)."""
    if role == "developer":
        return list(ROLES)
    return [r for r in ROLES if RANK[r] < RANK[role]]
