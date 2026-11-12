import json
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

import resend
from resend.exceptions import ResendError

KEYS = ["host", "port", "user", "password", "sender"]
CONFIG_FILE = Path(__file__).resolve().parent.parent / "smtp.json"


def _config():
    """SMTP settings from SMTP_* environment variables, falling back to smtp.json."""
    cfg = {k: os.environ.get(f"SMTP_{k.upper()}", "") for k in KEYS}
    try:
        file_cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        for k in KEYS:
            if not cfg[k] and file_cfg.get(k):
                cfg[k] = str(file_cfg[k])
    except (FileNotFoundError, ValueError):
        pass
    return cfg


def configured():
    resend_config = resend_current()
    if resend_config["has_api_key"]:
        return resend_config["configured"]
    c = _config()
    return bool(c["host"] and (c["sender"] or c["user"]))


def resend_current():
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    sender = os.environ.get("RESEND_FROM_EMAIL", "").strip()
    return {"has_api_key": bool(api_key), "sender": sender,
            "configured": bool(api_key and sender)}


def current():
    """Current settings with the password masked, for display."""
    c = _config()
    c["has_password"] = bool(c["password"])
    c.pop("password")
    return c


def env_overrides():
    smtp = [f"SMTP_{k.upper()}" for k in KEYS if os.environ.get(f"SMTP_{k.upper()}")]
    resend = [name for name in ("RESEND_API_KEY", "RESEND_FROM_EMAIL") if os.environ.get(name)]
    return smtp + resend


def using_resend():
    return bool(os.environ.get("RESEND_API_KEY", "").strip()
                and os.environ.get("RESEND_FROM_EMAIL", "").strip())


def save_config(host, port, user, password, sender):
    """Write smtp.json. An empty password keeps the stored one. Returns an error message or None."""
    host, user, sender = host.strip(), user.strip(), sender.strip()
    try:
        port = int(port)
        if not 1 <= port <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        return "Port must be a number between 1 and 65535."
    if not host or not (sender or user):
        return "A host and a sender address (or username) are required."
    try:
        old = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        old = {}
    data = {"host": host, "port": port, "user": user,
            "password": password or old.get("password", ""), "sender": sender}
    CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return None


def send_email(to, subject, html):
    """Send a plain-text email. Returns None on success or an error message."""
    if not to:
        return "No email address."
    resend_config = resend_current()
    if resend_config["has_api_key"]:
        if not resend_config["sender"]:
            return "RESEND_FROM_EMAIL is required when RESEND_API_KEY is set."
        resend.api_key = os.environ["RESEND_API_KEY"].strip()
        params = {
            "from": resend_config["sender"],
            "to": [to],
            "subject": subject,
            "html": html,
        }
        try:
            resend.Emails.send(params)
        except ResendError as e:
            return f"Could not send email: {e}"
        return None

    c = _config()
    if not configured():
        return "Email is not configured. Set RESEND_API_KEY and RESEND_FROM_EMAIL or add SMTP settings."
    msg = EmailMessage()
    msg["From"] = c["sender"] or c["user"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.add_alternative(html, subtype="html")
    port = int(c["port"] or 587)
    try:
        if port == 465:
            with smtplib.SMTP_SSL(c["host"], port, context=ssl.create_default_context(), timeout=15) as s:
                if c["user"]:
                    s.login(c["user"], c["password"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(c["host"], port, timeout=15) as s:
                s.starttls(context=ssl.create_default_context())
                if c["user"]:
                    s.login(c["user"], c["password"])
                s.send_message(msg)
    except Exception as e:
        return f"Could not send email: {e}"
    return None
