import json
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

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
    c = _config()
    return bool(c["host"] and (c["sender"] or c["user"]))


def send_email(to, subject, body):
    """Send a plain-text email. Returns None on success or an error message."""
    c = _config()
    if not configured():
        return "Email is not configured. Add SMTP settings (see smtp.example.json)."
    if not to:
        return "No email address."
    msg = EmailMessage()
    msg["From"] = c["sender"] or c["user"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
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
