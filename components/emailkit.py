"""Branded HTML emails: one shared layout plus deep links back into the site.

`render()` returns (html, text). Pass both to `mailer.send_email`.

Links need the site's public address. Inside a web request it is taken from the request; for emails sent
from the reminder script (no request) set APP_BASE_URL, e.g. https://assets.tgmeducation.com. With no
address available the button is left out rather than pointing at a broken link.
"""
import os
from pathlib import Path

from flask import has_request_context, request
from jinja2 import Environment, FileSystemLoader, select_autoescape

_ENV = Environment(
    loader=FileSystemLoader(str(Path(__file__).resolve().parent.parent / "templates" / "email")),
    autoescape=select_autoescape(["html"]),
)

# kind -> (accent colour, badge text). "account" covers sign-in, reset and admin messages.
STYLES = {
    "request": ("#0056b3", "Action needed"),
    "approval": ("#1f8f4e", "Decision"),
    "rejected": ("#c62828", "Decision"),
    "payment": ("#6f42c1", "Payment"),
    "reminder": ("#d98200", "Reminder"),
    "overdue": ("#c62828", "Overdue"),
    "info": ("#0f7b8a", "Update"),
    "account": ("#0056b3", "Account"),
}
PRIORITY_COLOURS = {"critical": "#c62828", "high": "#c62828", "medium": "#d98200", "low": "#1f8f4e"}


def site_url():
    base = os.environ.get("APP_BASE_URL", "").strip().rstrip("/")
    if not base and has_request_context():
        base = request.host_url.rstrip("/")
    return base


def link(path):
    """Absolute URL for a site path such as "/approvals", or "" when the site address is unknown."""
    base = site_url()
    return f"{base}{path}" if base and path else ""


def render(kind, heading, intro="", facts=(), button=None, note="", code=None, preheader=""):
    """Build one email. `facts` is a list of (label, value) pairs, `button` is (label, site path), and
    `code` (label, value) shows a large one-time code."""
    accent, badge = STYLES.get(kind, STYLES["info"])
    rows = []
    for label, value in facts:
        value = "" if value is None else str(value)
        if value:
            colour = PRIORITY_COLOURS.get(value.lower()) if label.lower() == "priority" else None
            rows.append({"label": label, "value": value, "chip": colour})
    url = link(button[1]) if button else ""
    button_label = button[0] if button else ""
    ctx = dict(accent=accent, badge=badge, heading=heading, intro=intro, facts=rows, code=code, note=note,
               button_url=url, button_label=button_label, home_url=link("/"),
               settings_url=link("/notifications"), preheader=preheader or intro or heading)
    html = _ENV.get_template("base.html").render(**ctx)

    lines = [heading, "", intro] if intro else [heading]
    if code:
        lines += ["", f"{code[0]}: {code[1]}"]
    if rows:
        lines.append("")
        lines += [f"{r['label']}: {r['value']}" for r in rows]
    if note:
        lines += ["", note]
    if url:
        lines += ["", f"{button_label}: {url}"]
    lines += ["", "TGM Assets · Maintenance & asset tracking"]
    return html, "\n".join(lines)
