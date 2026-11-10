import json
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

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
    resend = os.environ.get("RESEND_API_KEY", "").strip()
    resend_from = os.environ.get("RESEND_FROM_EMAIL", "").strip()
    return bool((resend and resend_from) or (c["host"] and (c["sender"] or c["user"])))


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


def send_email(to, subject, body):
    """Send a plain-text email. Returns None on success or an error message."""
    c = _config()
    if not configured():
        return "Email is not configured. Set RESEND_API_KEY and RESEND_FROM_EMAIL or configure SMTP."
    if not to:
        return "No email address."
    resend_key = os.environ.get("RESEND_API_KEY", "").strip()
    resend_from = os.environ.get("RESEND_FROM_EMAIL", "").strip()
    if resend_key and resend_from:
        payload = json.dumps({"from": resend_from, "to": [to],
                              "subject": subject, "text": body}).encode("utf-8")
        req = Request("https://api.resend.com/emails", data=payload,
                      headers={"Authorization": f"Bearer {resend_key}",
                               "Content-Type": "application/json"},
                      method="POST")
        try:
            with urlopen(req, timeout=15) as response:
                if response.status not in (200, 201):
                    return f"Resend returned unexpected HTTP status {response.status}."
        except HTTPError as e:
            detail = e.read(1000).decode("utf-8", errors="replace")
            return f"Resend returned HTTP {e.code}: {detail}"
        except (URLError, TimeoutError) as e:
            return f"Could not send email via Resend: {e}"
        return None
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
