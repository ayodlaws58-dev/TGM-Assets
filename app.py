import hmac
import io
import json
import math
import os
import re
import secrets
import threading
import time
import uuid
from datetime import date
from functools import wraps

import pandas as pd
import qrcode
from qrcode.image.svg import SvgPathImage
from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, send_file, session, url_for)
from werkzeug.utils import secure_filename

from components import auth, emailkit, mailer, notify, store, themes

BASE = store.DATA.parent
SECRET_FILE = store.DATA / ".secret_key"
WRITE_LOCK = threading.RLock()
FAILS = {}  # (ip, username) -> (count, first_fail_time)
MAX_FAILS, LOCK_SECONDS = 5, 300


def _secret_key():
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    if not SECRET_FILE.exists():
        SECRET_FILE.write_text(secrets.token_hex(32))
    return SECRET_FILE.read_text().strip()


app = Flask(__name__)
app.config.update(SECRET_KEY=_secret_key(), SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE="Lax", MAX_CONTENT_LENGTH=10 * 1024 * 1024)

# Behind Cloudflare Tunnel every request arrives from cloudflared on localhost. Trust its
# forwarded headers so sign-in lockouts see the visitor's address and links use https.
# Enable only when the app listens on 127.0.0.1 and nothing else can reach it.
if os.environ.get("TRUST_PROXY", "").strip().lower() in ("1", "true", "yes"):
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.config.update(SESSION_COOKIE_SECURE=True, PREFERRED_URL_SCHEME="https")


# ---------- request plumbing ----------
def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


app.jinja_env.globals.update(csrf_token=csrf_token, ROLE_LABEL=auth.ROLE_LABEL,
                             has_role=lambda r: bool(g.get("user")) and auth.RANK[g.user["role"]] >= auth.RANK[r],
                             KIND_LABEL=notify.KIND_LABEL, logo_url=lambda: url_for("logo"),
                             seasonal_theme=lambda: themes.theme_for_date(date.today()))


@app.before_request
def before():
    if request.method == "POST":
        maybe_run_reminders.last = 0  # data may change, so refresh reminders on the next page view
        WRITE_LOCK.acquire()
        g.locked = True
        sent = request.form.get("csrf_token", "")
        if not hmac.compare_digest(sent, session.get("csrf", "")):
            abort(400, "Invalid or missing CSRF token. Reload the page and try again.")
    g.user = None
    uname = session.get("username")
    if uname:
        users = auth.load_users()
        row = users[users["username"] == uname]
        if row.empty:  # deleted accounts lose access immediately
            session.clear()
        else:
            r = row.iloc[0]
            g.user = {"username": r["username"], "full_name": r["full_name"], "role": r["role"],
                      "email": r["email"]}
    if g.user:
        g.unread = notify.unread_count(g.user["username"])


@app.teardown_request
def after(_exc):
    if g.get("locked"):
        WRITE_LOCK.release()


@app.after_request
def headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'"
    return resp


def login_required(minimum="user"):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if not auth.load_users().shape[0]:
                return redirect(url_for("setup"))
            if not g.user:
                target = request.full_path.rstrip("?")
                if request.method == "GET" and target != "/":  # emailed links land on their page after sign-in
                    return redirect(url_for("login", next=target))
                return redirect(url_for("login"))
            if auth.RANK[g.user["role"]] < auth.RANK[minimum]:
                abort(403)
            return fn(*a, **kw)
        return wrapper
    return deco


def _safe_next(value):
    """Only same-site paths are allowed as a post-login destination (blocks open redirects)."""
    value = (value or "").strip()
    if value.startswith("/") and not value.startswith(("//", "/\\")) and "\n" not in value and "\r" not in value:
        return value
    return ""


@app.errorhandler(403)
def forbidden(_):
    return render_template("message.html", title="No permission",
                           text="You don't have permission to view this page."), 403


@app.errorhandler(400)
def bad_request(e):
    return render_template("message.html", title="Bad request", text=e.description), 400


@app.errorhandler(404)
def not_found(_):
    return render_template("message.html", title="Not found", text="That page or record doesn't exist."), 404


# ---------- helpers ----------
def today():
    return date.today().isoformat()


def num(value, default=0.0):
    try:
        v = float(value)
        return v if v >= 0 else default
    except (TypeError, ValueError):
        return default


def valid_date(value):
    try:
        date.fromisoformat(value)
        return value
    except (TypeError, ValueError):
        return ""


def schedule_rows(assets):
    assets = assets.copy()
    if "status" in assets:
        assets["lifecycle_status"] = assets["status"].replace("", "Active")
    df = store.with_due_dates(assets)
    df["next_service"] = df["next_service"].dt.strftime("%Y-%m-%d").fillna("")
    rows = df.to_dict("records")
    for r in rows:
        r["days_left"] = None if pd.isna(r["days_left"]) else int(r["days_left"])
    return rows


def money_fmt(v):
    return f"{v:,.2f}Naira"


app.jinja_env.filters["money"] = money_fmt


def maybe_run_reminders():
    # Reminders are generated lazily, at most once a minute, while people use the app.
    now = time.time()
    if now - getattr(maybe_run_reminders, "last", 0) > 60:
        maybe_run_reminders.last = now
        notify.run_reminders()


@app.before_request
def reminders():
    if g.get("user") and request.method == "GET" and request.endpoint != "static":
        with WRITE_LOCK:
            maybe_run_reminders()
            g.unread = notify.unread_count(g.user["username"])


# ---------- auth ----------
def _throttled(key):
    count, first = FAILS.get(key, (0, 0))
    if count >= MAX_FAILS and time.time() - first < LOCK_SECONDS:
        return True
    if time.time() - first >= LOCK_SECONDS:
        FAILS.pop(key, None)
    return False


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if auth.load_users().shape[0]:
        return redirect(url_for("login"))
    if request.method == "POST":
        err = auth.add_user(request.form["username"], request.form["full_name"], "developer",
                            request.form["password"], request.form.get("email", ""))
        if err:
            flash(err, "error")
        else:
            flash("Developer account created. Please sign in.", "success")
            return redirect(url_for("login"))
    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not auth.load_users().shape[0]:
        return redirect(url_for("setup"))
    nxt = _safe_next(request.values.get("next"))
    if g.user:
        return redirect(nxt or url_for("dashboard"))
    if request.method == "POST":
        uname = request.form["username"].strip().lower()
        key = (request.remote_addr, uname)
        if _throttled(key):
            flash("Too many failed attempts. Try again in a few minutes.", "error")
        else:
            r = auth.verify(uname, request.form["password"])
            if r is None:
                count, first = FAILS.get(key, (0, time.time()))
                FAILS[key] = (count + 1, first)
                flash("Invalid username or password.", "error")
            else:
                FAILS.pop(key, None)
                session.clear()
                session["username"] = r["username"]
                return redirect(nxt or url_for("dashboard"))
    return render_template("login.html", next=nxt)


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if not auth.load_users().shape[0]:
        return redirect(url_for("setup"))
    if g.user:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        key = (request.remote_addr, "signup")
        if _throttled(key):
            flash("Too many sign-up attempts. Try again in a few minutes.", "error")
        else:
            count, first = FAILS.get(key, (0, time.time()))
            FAILS[key] = (count + 1, first)
            err = auth.add_user(request.form.get("username", ""), request.form.get("full_name", ""), "user",
                                request.form.get("password", ""), request.form.get("email", ""))
            if err:
                flash(err, "error")
            else:
                FAILS.pop(key, None)
                flash("Account created. Please sign in.", "success")
                return redirect(url_for("login"))
    return render_template("signup.html")


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/forgot", methods=["GET", "POST"])
def forgot():
    if request.method == "POST":
        ident = request.form["identifier"]
        err = auth.request_reset(ident)
        if err:
            flash(err, "error")
        else:
            session["reset_ident"] = ident
            flash("If that account has an email on file, a code has been sent. It expires in 15 minutes.",
                  "success")
            return redirect(url_for("reset"))
    return render_template("forgot.html")


@app.route("/reset", methods=["GET", "POST"])
def reset():
    if request.method == "POST":
        err = auth.reset_with_code(request.form["identifier"], request.form["code"], request.form["password"])
        if err:
            flash(err, "error")
        else:
            session.pop("reset_ident", None)
            flash("Password updated. You can sign in now.", "success")
            return redirect(url_for("login"))
    return render_template("reset.html", identifier=session.get("reset_ident", ""))


# ---------- dashboard ----------
@app.route("/")
@login_required()
def dashboard():
    assets = store.load_assets()
    orders = store.load_work_orders()
    procurement = store.load_procurement()
    rows = schedule_rows(assets)
    approved = orders[orders["approval_status"] == "Approved"]
    open_orders = approved[approved["status"] != "Completed"]
    late = open_orders[(open_orders["due"] != "") & (open_orders["due"] < today())]
    cost = store.money(approved["cost"])
    paid = cost[approved["payment_status"] == "Paid"].sum()
    unpaid = cost[approved["payment_status"] != "Paid"].sum()
    committed = store.pr_total(procurement)[procurement["status"].isin(store.PR_COMMITTED)].sum()
    attention = [r for r in rows if r["status"] in ("Overdue", "Due soon")]
    by_status = approved["status"].value_counts().reindex(store.STATUSES, fill_value=0)
    return render_template(
        "dashboard.html",
        stats=dict(total=len(orders), open=len(open_orders), late=len(late),
                   pending_wo=int((orders["approval_status"] == "Pending").sum()),
                   cost=float(cost.sum()), paid=float(paid), unpaid=float(unpaid),
                   committed=float(committed),
                   pending_pr=int((procurement["status"] == "Pending").sum()),
                   overdue_assets=sum(r["status"] == "Overdue" for r in rows)),
        serv=sorted([r for r in attention if r["kind"] == "Servicing"], key=lambda r: r["days_left"]),
        paint=sorted([r for r in attention if r["kind"] == "Repainting"], key=lambda r: r["days_left"]),
        chart_status=[(k, int(v)) for k, v in by_status.items()],
        chart_pay=[("Paid", float(paid)), ("Unpaid", float(unpaid))])


# ---------- assets ----------
@app.route("/assets")
@login_required()
def assets_page():
    q = request.args.get("q", "").strip().lower()
    cat = request.args.get("category", "")
    all_assets = store.load_assets()
    rows = schedule_rows(all_assets)
    if q:
        rows = [r for r in rows
                if q in f"{r['name']} {r['location']} {r['category']} {r['owner']} {r['asset_id']}".lower()]
    if cat in store.CATEGORIES:
        rows = [r for r in rows if r["category"] == cat]
    rows.sort(key=lambda r: (r["next_service"] == "", r["next_service"]))
    return render_template("assets.html", rows=rows, parent_assets=all_assets.to_dict("records"),
                           q=q, cat=cat, categories=store.CATEGORIES)


def _asset_fields(form, assets, current_id=""):
    name = form.get("name", "").strip()
    category = form.get("category", "")
    try:
        interval = int(form.get("interval_days", ""))
    except (TypeError, ValueError):
        interval = 0
    last = valid_date(form.get("last_service", ""))
    parent_id = form.get("parent_id", "").strip()
    purchase_date = form.get("purchase_date", "").strip()
    status = form.get("status", "Active")
    life = form.get("useful_life_years", "").strip()

    def amount(field):
        raw = form.get(field, "").strip()
        if not raw:
            return ""
        try:
            value = float(raw)
        except ValueError:
            return None
        return f"{value:.2f}" if math.isfinite(value) and value >= 0 else None

    cost = amount("cost")
    salvage = amount("salvage_value")
    disposal_value = amount("disposal_value")
    retired_on = form.get("retired_on", "").strip()
    disposed_on = form.get("disposed_on", "").strip()
    if (not name or category not in store.CATEGORIES or interval < 1 or not last
            or status not in store.ASSET_STATUSES
            or (parent_id and (parent_id == current_id or not (assets["asset_id"] == parent_id).any()))
            or (purchase_date and not valid_date(purchase_date))
            or any(value is None for value in (cost, salvage, disposal_value))
            or (life and (not life.isdigit() or len(life) > 3 or not 1 <= int(life) <= 100))
            or (retired_on and not valid_date(retired_on))
            or (disposed_on and not valid_date(disposed_on))
            or (status == "Retired" and not valid_date(retired_on))
            or (status == "Disposed" and not valid_date(disposed_on))):
        return None
    if cost and salvage and float(salvage) > float(cost):
        return None
    if status != "Retired":
        retired_on = ""
    if status != "Disposed":
        disposed_on = ""
        disposal_value = ""
    if purchase_date and ((retired_on and retired_on < purchase_date)
                          or (disposed_on and disposed_on < purchase_date)):
        return None

    ancestor = parent_id
    visited = set()
    while ancestor:
        if ancestor == current_id or ancestor in visited:
            return None
        visited.add(ancestor)
        parent = assets[assets["asset_id"] == ancestor]
        ancestor = parent.iloc[0]["parent_id"] if not parent.empty else ""
    return dict(name=name, category=category, location=form.get("location", "").strip(),
                last_service=last, interval_days=interval, notes=form.get("notes", "").strip(),
                parent_id=parent_id, owner=form.get("owner", "").strip(),
                purchase_date=valid_date(purchase_date), cost=cost, status=status,
                useful_life_years=life, salvage_value=salvage, retired_on=valid_date(retired_on),
                disposed_on=valid_date(disposed_on), disposal_value=disposal_value,
                disposal_note=form.get("disposal_note", "").strip())


@app.post("/assets/new")
@login_required("super_admin")
def asset_new():
    assets = store.load_assets()
    fields = _asset_fields(request.form, assets)
    if not fields:
        flash("Check the asset details, parent, dates, status and non-negative financial values.", "error")
    else:
        assets = store.add_row(assets, asset_id=store.next_id(assets, "asset_id", "AST", 1), **fields)
        store.save_assets(assets)
        flash("Asset added.", "success")
    return redirect(url_for("assets_page"))


@app.route("/assets/<asset_id>/edit", methods=["GET", "POST"])
@login_required("super_admin")
def asset_edit(asset_id):
    assets = store.load_assets()
    i = assets["asset_id"] == asset_id
    if not i.any():
        abort(404)
    if request.method == "POST":
        fields = _asset_fields(request.form, assets, current_id=asset_id)
        if not fields:
            flash("Check the asset details, parent, dates, status and non-negative financial values.", "error")
        else:
            for k, v in fields.items():
                assets.loc[i, k] = str(v)
            store.save_assets(assets)
            flash("Asset updated.", "success")
            return redirect(url_for("assets_page"))
    return render_template("asset_edit.html", a=assets[i].iloc[0].to_dict(), categories=store.CATEGORIES,
                           statuses=store.ASSET_STATUSES,
                           parents=assets[assets["asset_id"] != asset_id].to_dict("records"))


@app.post("/assets/<asset_id>/delete")
@login_required("super_admin")
def asset_delete(asset_id):
    assets = store.load_assets()
    if not (assets["asset_id"] == asset_id).any():
        abort(404)
    if ((store.load_schedules()["asset_id"] == asset_id).any()
            or (store.load_service_history()["asset_id"] == asset_id).any()
            or (store.load_work_orders()["asset_id"] == asset_id).any()):
        flash("Retire the asset instead of deleting it so its maintenance history is preserved.", "error")
        return redirect(url_for("assets_page"))
    if (assets["parent_id"] == asset_id).any():
        flash("Move or delete child assets before deleting this asset.", "error")
        return redirect(url_for("assets_page"))
    attachment_ids = [item["id"] for item in _asset_attachment_rows(
        assets.loc[assets["asset_id"] == asset_id].iloc[0].to_dict())]
    store.save_assets(assets[assets["asset_id"] != asset_id])
    for attachment_id in attachment_ids:
        if re.fullmatch(r"[a-f0-9]{32}\.(?:png|jpg|jpeg|webp|pdf)", attachment_id):
            (store.DATA / "asset_uploads" / attachment_id).unlink(missing_ok=True)
    flash("Asset deleted.", "success")
    return redirect(url_for("assets_page"))


def _asset_attachment_rows(asset):
    return json.loads(asset.get("attachments") or "[]")


def _asset_depreciation(asset, as_of=None):
    cost = num(asset.get("cost", ""), None)
    salvage = num(asset.get("salvage_value", ""), None)
    try:
        life = int(asset.get("useful_life_years") or 0)
    except ValueError:
        life = 0
    purchase = valid_date(asset.get("purchase_date", ""))
    if (cost is None or salvage is None or not math.isfinite(cost) or not math.isfinite(salvage)
            or not life or not purchase or cost < salvage):
        return None
    as_of = as_of or date.today()
    stop = as_of
    for field in ("retired_on", "disposed_on"):
        end = valid_date(asset.get(field, ""))
        if end:
            stop = min(stop, date.fromisoformat(end))
    elapsed_years = max(0, (stop - date.fromisoformat(purchase)).days / 365.2425)
    annual = (cost - salvage) / life
    accumulated = min(cost - salvage, annual * elapsed_years)
    return {"annual": annual, "accumulated": accumulated,
            "book_value": max(salvage, cost - accumulated)}


@app.route("/assets/<asset_id>")
@login_required()
def asset_detail(asset_id):
    assets = store.load_assets()
    matches = assets[assets["asset_id"] == asset_id]
    if matches.empty:
        abort(404)
    asset = matches.iloc[0].to_dict()
    attachments = _asset_attachment_rows(asset)
    children = assets[assets["parent_id"] == asset_id].to_dict("records")
    parent = assets[assets["asset_id"] == asset["parent_id"]]
    services = store.load_service_history()
    services = services[services["asset_id"] == asset_id].sort_values(
        ["service_date", "created"], ascending=False)
    schedules = store.load_schedules()
    schedules = schedules[schedules["asset_id"] == asset_id]
    return render_template("asset_detail.html", a=asset, attachments=attachments,
                           children=children, parent=parent.iloc[0].to_dict() if not parent.empty else None,
                           depreciation=_asset_depreciation(asset),
                           services=services.to_dict("records"),
                           schedules=schedules.to_dict("records"))


@app.route("/assets/<asset_id>/label")
@login_required()
def asset_label(asset_id):
    assets = store.load_assets()
    matches = assets[assets["asset_id"] == asset_id]
    if matches.empty:
        abort(404)
    return render_template("asset_label.html", a=matches.iloc[0].to_dict())


@app.route("/assets/<asset_id>/qr.svg")
@login_required()
def asset_qr(asset_id):
    if not (store.load_assets()["asset_id"] == asset_id).any():
        abort(404)
    image = qrcode.make(url_for("asset_detail", asset_id=asset_id, _external=True),
                        image_factory=SvgPathImage, box_size=5, border=2)
    output = io.BytesIO()
    image.save(output)
    output.seek(0)
    return send_file(output, mimetype="image/svg+xml", download_name=f"{asset_id}.svg")


UPLOAD_LIMIT = 8 * 1024 * 1024
UPLOAD_TYPES = {
    ".png": ("image/png", b"\x89PNG\r\n\x1a\n", "photo"),
    ".jpg": ("image/jpeg", b"\xff\xd8\xff", "photo"),
    ".jpeg": ("image/jpeg", b"\xff\xd8\xff", "photo"),
    ".webp": ("image/webp", b"RIFF", "photo"),
    ".pdf": ("application/pdf", b"%PDF-", "document"),
}


@app.post("/assets/<asset_id>/attachments")
@login_required("super_admin")
def asset_attachment_upload(asset_id):
    assets = store.load_assets()
    i = assets["asset_id"] == asset_id
    if not i.any():
        abort(404)
    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files or len(files) > 5:
        flash("Select between one and five photos or PDF documents.", "error")
        return redirect(url_for("asset_detail", asset_id=asset_id))
    prepared = []
    for file in files:
        name = secure_filename(file.filename)
        ext = os.path.splitext(name)[1].lower()
        kind = UPLOAD_TYPES.get(ext)
        data = file.read(UPLOAD_LIMIT + 1)
        if (not name or not kind or not data or len(data) > UPLOAD_LIMIT
                or not data.startswith(kind[1])
                or (ext == ".webp" and data[8:12] != b"WEBP")):
            flash("Uploads must be valid PNG, JPG, WebP or PDF files under 8 MB each.", "error")
            return redirect(url_for("asset_detail", asset_id=asset_id))
        prepared.append((name, kind, data))

    attachment_dir = store.DATA / "asset_uploads"
    attachment_dir.mkdir(parents=True, exist_ok=True)
    attachments = _asset_attachment_rows(assets.loc[i].iloc[0].to_dict())
    written = []
    try:
        for name, kind, data in prepared:
            attachment_id = uuid.uuid4().hex + os.path.splitext(name)[1].lower()
            path = attachment_dir / attachment_id
            path.write_bytes(data)
            written.append(path)
            attachments.append({"id": attachment_id, "name": name, "mime": kind[0],
                                "type": kind[2], "created": today()})
        assets.loc[i, "attachments"] = json.dumps(attachments)
        store.save_assets(assets)
    except Exception:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    flash(f"Uploaded {len(prepared)} attachment(s).", "success")
    return redirect(url_for("asset_detail", asset_id=asset_id))


@app.route("/assets/<asset_id>/attachments/<attachment_id>")
@login_required()
def asset_attachment(asset_id, attachment_id):
    if not re.fullmatch(r"[a-f0-9]{32}\.(?:png|jpg|jpeg|webp|pdf)", attachment_id):
        abort(404)
    assets = store.load_assets()
    matches = assets[assets["asset_id"] == asset_id]
    if matches.empty:
        abort(404)
    attachment = next((item for item in _asset_attachment_rows(matches.iloc[0].to_dict())
                       if item["id"] == attachment_id), None)
    path = store.DATA / "asset_uploads" / attachment_id
    if not attachment or not path.is_file():
        abort(404)
    return send_file(path, mimetype=attachment["mime"],
                     as_attachment=attachment["type"] == "document",
                     download_name=attachment["name"])


@app.post("/assets/<asset_id>/attachments/<attachment_id>/delete")
@login_required("super_admin")
def asset_attachment_delete(asset_id, attachment_id):
    if not re.fullmatch(r"[a-f0-9]{32}\.(?:png|jpg|jpeg|webp|pdf)", attachment_id):
        abort(404)
    assets = store.load_assets()
    i = assets["asset_id"] == asset_id
    if not i.any():
        abort(404)
    attachments = _asset_attachment_rows(assets.loc[i].iloc[0].to_dict())
    kept = [item for item in attachments if item["id"] != attachment_id]
    if len(kept) == len(attachments):
        abort(404)
    assets.loc[i, "attachments"] = json.dumps(kept)
    store.save_assets(assets)
    (store.DATA / "asset_uploads" / attachment_id).unlink(missing_ok=True)
    flash("Attachment removed.", "success")
    return redirect(url_for("asset_detail", asset_id=asset_id))


# ---------- maintenance / work orders ----------
@app.route("/maintenance")
@login_required()
def maintenance():
    assets = store.load_assets()
    rows = schedule_rows(assets)
    alerts = sorted([r for r in rows if r["status"] in ("Overdue", "Due soon")], key=lambda r: r["days_left"])
    all_schedules = store.load_schedules()
    calendar_rows = store.schedule_rows(all_schedules, assets)
    services = store.load_service_history().sort_values(
        ["service_date", "created"], ascending=False).head(100)
    active_assets = assets[~assets["status"].isin(["Retired", "Disposed"])]
    return render_template("maintenance.html", alerts=alerts, schedules=calendar_rows,
                           all_schedules=all_schedules.to_dict("records"),
                           services=services.to_dict("records"),
                           assets=active_assets.to_dict("records"), today=today(),
                           assignees=auth.load_users()["username"].tolist(),
                           schedule_units=store.SCHEDULE_UNITS,
                           stats=dict(overdue=sum(r["status"] == "Overdue" for r in rows)
                                      + sum(r["status"] == "Overdue" for r in calendar_rows),
                                      soon=sum(r["status"] == "Due soon" for r in rows)
                                      + sum(r["status"] == "Due soon" for r in calendar_rows)))


def _nonnegative_value(raw):
    raw = str(raw or "").strip()
    if not raw:
        return ""
    try:
        value = float(raw)
    except ValueError:
        return None
    return f"{value:.2f}" if math.isfinite(value) and value >= 0 else None


def _record_service(asset_id, service_date, summary, completion_notes="", work_order_id="",
                    schedule_id="", cost="", downtime_hours="", recorded_by=""):
    assets = store.load_assets()
    asset_mask = assets["asset_id"] == asset_id
    if not asset_mask.any() or assets.loc[asset_mask, "status"].iloc[0] in ("Retired", "Disposed"):
        abort(400, "A service record requires an active asset.")
    if not valid_date(service_date) or not summary.strip():
        abort(400, "A service date and summary are required.")
    history = store.load_service_history()
    if work_order_id and (history["work_order_id"] == work_order_id).any():
        abort(400, "This work order already has a service record.")
    schedules = store.load_schedules()
    if schedule_id:
        schedule_mask = schedules["schedule_id"] == schedule_id
        if not schedule_mask.any() or schedules.loc[schedule_mask, "asset_id"].iloc[0] != asset_id:
            abort(400, "The selected schedule does not belong to this asset.")
        schedules.loc[schedule_mask, "last_completed"] = service_date
        store.save_schedules(schedules)
    history = store.add_row(
        history, service_id=store.next_id(history, "service_id", "SRV", 1001),
        asset_id=asset_id, work_order_id=work_order_id, schedule_id=schedule_id,
        service_date=service_date, summary=summary.strip(),
        completion_notes=completion_notes.strip(), cost=cost,
        downtime_hours=downtime_hours, recorded_by=recorded_by, created=today())
    store.save_service_history(history)
    assets.loc[asset_mask, "last_service"] = service_date
    store.save_assets(assets)


@app.post("/maintenance/schedules/new")
@login_required("admin")
def schedule_new():
    assets = store.load_assets()
    asset_id = request.form.get("asset_id", "")
    title = request.form.get("title", "").strip()
    raw_interval = request.form.get("interval_value", "")
    unit = request.form.get("interval_unit", "")
    raw_last_completed = request.form.get("last_completed", "").strip()
    last_completed = valid_date(raw_last_completed)
    assignee = request.form.get("assigned_to", "").strip()
    selected = assets[assets["asset_id"] == asset_id]
    try:
        interval = int(raw_interval)
    except (TypeError, ValueError):
        interval = 0
    if (selected.empty or selected.iloc[0]["status"] in ("Retired", "Disposed")
            or not title or len(title) > 160 or not 1 <= interval <= 1000
            or unit not in store.SCHEDULE_UNITS
            or (raw_last_completed and not last_completed)
            or (assignee and not (auth.load_users()["username"] == assignee).any())):
        flash("Choose an active asset, enter a schedule name and a valid calendar interval.", "error")
        return redirect(url_for("maintenance"))
    schedules = store.load_schedules()
    schedules = store.add_row(
        schedules, schedule_id=store.next_id(schedules, "schedule_id", "SCH", 3001),
        asset_id=asset_id, title=title, interval_value=interval, interval_unit=unit,
        last_completed=last_completed or selected.iloc[0]["last_service"],
        assigned_to=assignee,
        description=request.form.get("description", "").strip(), active="1",
        created_by=g.user["username"])
    store.save_schedules(schedules)
    flash("Preventive schedule created.", "success")
    return redirect(url_for("maintenance"))


@app.post("/maintenance/schedules/<schedule_id>/toggle")
@login_required("admin")
def schedule_toggle(schedule_id):
    schedules = store.load_schedules()
    i = schedules["schedule_id"] == schedule_id
    if not i.any():
        abort(404)
    schedules.loc[i, "active"] = "0" if schedules.loc[i, "active"].iloc[0] == "1" else "1"
    store.save_schedules(schedules)
    flash("Schedule updated.", "success")
    return redirect(url_for("maintenance"))


@app.post("/maintenance/record")
@login_required("admin")
def record_service():
    assets = store.load_assets()
    when = valid_date(request.form.get("done_on", ""))
    asset_id = request.form.get("asset_id", "")
    cost = _nonnegative_value(request.form.get("cost", ""))
    downtime = _nonnegative_value(request.form.get("downtime_hours", ""))
    if cost is None or downtime is None or not when or not (assets["asset_id"] == asset_id).any():
        flash("Choose an asset and enter a valid date, cost and downtime.", "error")
        return redirect(url_for("maintenance"))
    schedule_id = request.form.get("schedule_id", "")
    if schedule_id:
        schedules = store.load_schedules()
        selected_schedule = schedules[schedules["schedule_id"] == schedule_id]
        if (selected_schedule.empty or selected_schedule.iloc[0]["asset_id"] != asset_id
                or selected_schedule.iloc[0]["active"] != "1"):
            flash("Choose an active preventive schedule for the selected asset.", "error")
            return redirect(url_for("maintenance"))
    _record_service(asset_id, when, request.form.get("summary", ""),
                    request.form.get("completion_notes", ""),
                    schedule_id=schedule_id,
                    cost=cost, downtime_hours=downtime, recorded_by=g.user["username"])
    flash("Service record saved.", "success")
    return redirect(url_for("maintenance"))


@app.route("/workorders")
@login_required()
def workorders():
    orders = store.load_work_orders()
    assets = store.load_assets()
    board = {s: orders[orders["status"] == s].to_dict("records") for s in store.STATUSES}
    assets = assets[~assets["status"].isin(["Retired", "Disposed"])]
    schedules = store.load_schedules()
    users = auth.load_users()["username"].tolist()
    return render_template("workorders.html", board=board, statuses=store.STATUSES, today=today(),
                           assets=assets.to_dict("records"), priorities=store.PRIORITIES,
                           schedules=schedules[schedules["active"] == "1"].to_dict("records"),
                           assignees=users)


@app.post("/workorders/new")
@login_required()
def workorder_new():
    assets = store.load_assets()
    orders = store.load_work_orders()
    aid = request.form.get("asset_id", "")
    title = request.form.get("title", "").strip()
    selected = assets[assets["asset_id"] == aid]
    if not title or selected.empty:
        flash("A title and a valid asset are required.", "error")
        return redirect(url_for("workorders"))
    if selected.iloc[0]["status"] in ("Retired", "Disposed"):
        flash("Work orders cannot be created for retired or disposed assets.", "error")
        return redirect(url_for("workorders"))
    assigned_to = request.form.get("assigned_to", "").strip()
    if assigned_to and not (auth.load_users()["username"] == assigned_to).any():
        flash("Choose a valid staff member for assignment.", "error")
        return redirect(url_for("workorders"))
    schedule_id = request.form.get("schedule_id", "").strip()
    if schedule_id:
        schedule = store.load_schedules()
        match = schedule[schedule["schedule_id"] == schedule_id]
        if match.empty or match.iloc[0]["asset_id"] != aid or match.iloc[0]["active"] != "1":
            flash("Choose a preventive schedule for the selected asset.", "error")
            return redirect(url_for("workorders"))
    prio = request.form.get("priority", "Medium")
    wo_id = store.next_id(orders, "wo_id", "WO", 1001)
    orders = store.add_row(
        orders, wo_id=wo_id, asset_id=aid, title=title,
        description=request.form.get("description", "").strip(),
        priority=prio if prio in store.PRIORITIES else "Medium", status="New",
        assigned_to=assigned_to, schedule_id=schedule_id, created=today(),
        due=valid_date(request.form.get("due", "")), requested_by=g.user["username"],
        cost=num(request.form.get("cost")), payment_status="Unpaid", approval_status="Pending")
    store.save_work_orders(orders)
    notify.send(notify.recipients("admin", exclude=[g.user["username"]]), "request",
                "Work order awaiting approval", f"{title} requested by {g.user['username']}.",
                link=("Review work order", "/approvals"),
                facts=[("Work order", wo_id), ("Asset", f"{aid} {selected.iloc[0]['name']}"),
                       ("Priority", prio if prio in store.PRIORITIES else "Medium"),
                       ("Due", valid_date(request.form.get("due", ""))),
                       ("Requested by", g.user["full_name"] or g.user["username"])])
    flash("Work order submitted for approval.", "success")
    return redirect(url_for("workorders"))


@app.post("/workorders/<wo_id>/status")
@login_required("admin")
def workorder_status(wo_id):
    orders = store.load_work_orders()
    i = (orders["wo_id"] == wo_id) & (orders["approval_status"] == "Approved")
    status = request.form.get("status")
    if not i.any() or status not in store.STATUSES:
        abort(400, "That work order can't be updated (it may not be approved yet).")
    if orders.loc[i, "status"].iloc[0] == "Completed":
        abort(400, "Completed work orders cannot be changed.")
    actual_cost = _nonnegative_value(request.form.get("actual_cost", ""))
    downtime = _nonnegative_value(request.form.get("downtime_hours", ""))
    completion_notes = request.form.get("completion_notes", "").strip()
    if actual_cost is None or downtime is None:
        abort(400, "Actual cost and downtime must be non-negative numbers.")
    if status == "Completed" and not completion_notes:
        abort(400, "Completion notes are required to complete a work order.")
    orders.loc[i, "status"] = status
    orders.loc[i, "completed"] = today() if status == "Completed" else ""
    if status == "Completed":
        row = orders.loc[i].iloc[0]
        orders.loc[i, "completion_notes"] = completion_notes
        orders.loc[i, "actual_cost"] = actual_cost or row["cost"]
        orders.loc[i, "cost"] = actual_cost or row["cost"]
        orders.loc[i, "downtime_hours"] = downtime
        _record_service(row["asset_id"], today(), row["title"], completion_notes,
                        work_order_id=wo_id, schedule_id=row["schedule_id"],
                        cost=orders.loc[i, "actual_cost"].iloc[0], downtime_hours=downtime,
                        recorded_by=g.user["username"])
    store.save_work_orders(orders)
    return redirect(url_for("workorders"))


@app.route("/payments")
@login_required("admin")
def payments():
    orders = store.load_work_orders()
    pay = orders[orders["approval_status"] == "Approved"]
    amounts = store.money(pay["cost"])
    return render_template("payments.html", rows=pay.to_dict("records"),
                           total=float(amounts.sum()),
                           paid=float(amounts[pay["payment_status"] == "Paid"].sum()),
                           unpaid=float(amounts[pay["payment_status"] != "Paid"].sum()),
                           pay_statuses=store.PAYMENT_STATUSES)


@app.post("/payments/<wo_id>")
@login_required("admin")
def payment_save(wo_id):
    orders = store.load_work_orders()
    i = (orders["wo_id"] == wo_id) & (orders["approval_status"] == "Approved")
    pstatus = request.form.get("payment_status")
    if not i.any() or pstatus not in store.PAYMENT_STATUSES:
        abort(400, "That work order can't take a payment (it may not be approved yet).")
    amount = num(request.form.get("cost"))
    was_paid = orders.loc[i, "payment_status"].iloc[0] == "Paid"
    orders.loc[i, "cost"] = str(amount)
    orders.loc[i, "payment_status"] = pstatus
    orders.loc[i, "paid_on"] = today() if pstatus == "Paid" else ""
    store.save_work_orders(orders)
    requester = orders.loc[i, "requested_by"].iloc[0]
    if pstatus == "Paid" and not was_paid and requester and requester != g.user["username"]:
        notify.send([requester], "payment", f"Payment recorded: {wo_id}", f"{amount:,.2f} Naira marked as paid.",
                    link=("View work order", "/workorders"),
                    facts=[("Work order", wo_id), ("Amount", f"{amount:,.2f} Naira"), ("Paid on", today())])
    flash("Payment saved.", "success")
    return redirect(url_for("payments"))


# ---------- procurement ----------
@app.route("/procurement")
@login_required()
def procurement():
    reqs = store.load_procurement()
    if not auth.RANK[g.user["role"]] >= auth.RANK["admin"]:
        reqs = reqs[reqs["requested_by"] == g.user["username"]]
    reqs = reqs.assign(total=store.pr_total(reqs))
    committed = float(reqs.loc[reqs["status"].isin(store.PR_COMMITTED), "total"].sum())
    return render_template("procurement.html", rows=reqs.iloc[::-1].to_dict("records"),
                           assets=store.load_assets().to_dict("records"), suggestions=store.PROCUREMENT_ITEMS,
                           orders=store.load_work_orders()["wo_id"].tolist(),
                           stats=dict(n=len(reqs), pending=int((reqs["status"] == "Pending").sum()),
                                      active=int(reqs["status"].isin(["Approved", "Ordered"]).sum()),
                                      committed=committed))


@app.post("/procurement/new")
@login_required()
def procurement_new():
    item = request.form.get("item", "").strip()
    qty = int(num(request.form.get("quantity"), 0))
    if not item or qty < 1:
        flash("An item and a quantity of at least 1 are required.", "error")
        return redirect(url_for("procurement"))
    reqs = store.load_procurement()
    aid = request.form.get("asset_id", "")
    wo = request.form.get("wo_id", "")
    if aid and not (store.load_assets()["asset_id"] == aid).any():
        aid = ""
    if wo and not (store.load_work_orders()["wo_id"] == wo).any():
        wo = ""
    req_id = store.next_id(reqs, "req_id", "PR", 2001)
    unit_cost = num(request.form.get("unit_cost"))
    reason = request.form.get("reason", "").strip()
    reqs = store.add_row(
        reqs, req_id=req_id, item=item, quantity=qty,
        unit_cost=unit_cost, asset_id=aid, wo_id=wo,
        reason=reason, requested_by=g.user["username"],
        created=today(), status="Pending")
    store.save_procurement(reqs)
    # Procurement requests carry no department or priority of their own; use the linked work order's priority if any.
    orders = store.load_work_orders()
    linked = orders[orders["wo_id"] == wo] if wo else orders.iloc[0:0]
    notify.send(notify.recipients("admin", exclude=[g.user["username"]]), "request",
                "Procurement request awaiting approval", f"{item} × {qty} requested by {g.user['username']}.",
                link=("Review request", "/approvals"),
                facts=[("Request", req_id), ("Requested by", g.user["full_name"] or g.user["username"]),
                       ("Item", f"{item} × {qty}"), ("Reason", reason),
                       ("Priority", linked.iloc[0]["priority"] if not linked.empty else ""),
                       ("Total cost", f"{qty * unit_cost:,.2f} Naira" if unit_cost else "")])
    flash("Request submitted for approval.", "success")
    return redirect(url_for("procurement"))


@app.post("/procurement/<req_id>/advance")
@login_required("admin")
def procurement_advance(req_id):
    step = {"Approved": "Ordered", "Ordered": "Received"}
    reqs = store.load_procurement()
    i = reqs["req_id"] == req_id
    if not i.any() or reqs.loc[i, "status"].iloc[0] not in step:
        abort(400, "That request can't be advanced.")
    new = step[reqs.loc[i, "status"].iloc[0]]
    reqs.loc[i, "status"] = new
    store.save_procurement(reqs)
    notify.send([reqs.loc[i, "requested_by"].iloc[0]], "info", f"{req_id} is now {new}",
                f"Your request for {reqs.loc[i, 'item'].iloc[0]} was updated.",
                link=("View request", "/procurement"),
                facts=[("Request", req_id), ("Item", reqs.loc[i, "item"].iloc[0]), ("New status", new)])
    return redirect(url_for("procurement"))


# ---------- approvals ----------
def _may_decide(requester):
    # Nobody approves their own request, except a Developer.
    return requester != g.user["username"] or g.user["role"] == "developer"


@app.route("/approvals")
@login_required("admin")
def approvals():
    orders = store.load_work_orders()
    reqs = store.load_procurement()
    pending_wo = orders[orders["approval_status"] == "Pending"]
    pending_pr = reqs[reqs["status"] == "Pending"].assign(total=lambda d: store.pr_total(d))
    return render_template(
        "approvals.html", pending_wo=pending_wo.to_dict("records"), pending_pr=pending_pr.to_dict("records"),
        hist_wo=orders[orders["approval_status"] != "Pending"].iloc[::-1].to_dict("records"),
        hist_pr=reqs[reqs["status"] != "Pending"].iloc[::-1].to_dict("records"), may_decide=_may_decide)


def _decide(kind, item_id):
    decision = request.form.get("decision")
    note = request.form.get("note", "").strip()
    if decision not in ("Approved", "Rejected"):
        abort(400, "Invalid decision.")
    if decision == "Rejected" and not note:
        flash("Add a note explaining the rejection.", "error")
        return redirect(url_for("approvals"))
    if kind == "wo":
        df, idcol, statcol, label = store.load_work_orders(), "wo_id", "approval_status", "title"
        cols = ["approval_status", "approved_by", "approved_on", "decision_note"]
        save = store.save_work_orders
    else:
        df, idcol, statcol, label = store.load_procurement(), "req_id", "status", "item"
        cols = ["status", "decided_by", "decided_on", "decision_note"]
        save = store.save_procurement
    i = (df[idcol] == item_id) & (df[statcol] == "Pending")
    if not i.any():
        abort(400, "That request was already decided.")
    requester = df.loc[i, "requested_by"].iloc[0]
    if not _may_decide(requester):
        abort(403)
    df.loc[i, cols] = [decision, g.user["username"], today(), note]
    save(df)
    notify.send([requester], "approval", f"{item_id} {decision.lower()}",
                f"{df.loc[i, label].iloc[0]} was {decision.lower()} by {g.user['username']}."
                + (f" Note: {note}" if note else ""),
                tone="rejected" if decision == "Rejected" else "approval",
                link=("View work order", "/workorders") if kind == "wo" else ("View request", "/procurement"),
                facts=[("Reference", item_id), ("Decision", decision),
                       ("Decided by", g.user["full_name"] or g.user["username"]), ("Note", note)])
    if kind == "wo" and decision == "Approved":
        order = df.loc[i].iloc[0]
        technician = order["assigned_to"]
        if technician:
            assets = store.load_assets()
            asset = assets[assets["asset_id"] == order["asset_id"]]
            staff = auth.load_users().set_index("username")
            who = staff["full_name"].get(technician, "") or technician
            notify.send([technician], "info", f"Work order {item_id} assigned to you",
                        f"Hello {who}, a new work order has been assigned to you: {order['title']}.",
                        link=("View work order", "/workorders"),
                        facts=[("Work order", item_id),
                               ("Asset", asset.iloc[0]["name"] if not asset.empty else order["asset_id"]),
                               ("Location", asset.iloc[0]["location"] if not asset.empty else ""),
                               ("Priority", order["priority"]), ("Due", order["due"]),
                               ("Details", order["description"])])
    flash(f"{item_id} {decision.lower()}.", "success")
    return redirect(url_for("approvals"))


@app.post("/approvals/wo/<wo_id>")
@login_required("admin")
def decide_wo(wo_id):
    return _decide("wo", wo_id)


@app.post("/approvals/pr/<req_id>")
@login_required("admin")
def decide_pr(req_id):
    return _decide("pr", req_id)


# ---------- notifications ----------
@app.route("/notifications")
@login_required()
def notifications():
    items = notify.for_user(g.user["username"])
    return render_template("notifications.html", items=items.head(100).to_dict("records"),
                           has_read=bool((items["read"] == "1").any()))


@app.post("/notifications/read")
@login_required()
def notifications_read():
    ids = request.form.getlist("id")
    notify.mark_read(g.user["username"], ids or None)
    return redirect(url_for("notifications"))


@app.post("/notifications/clear")
@login_required()
def notifications_clear():
    notify.clear_read(g.user["username"])
    return redirect(url_for("notifications"))


# ---------- users ----------
@app.route("/users")
@login_required("admin")
def users_page():
    users = auth.load_users()
    allowed = auth.manageable_roles(g.user["role"])
    editable = [u for u in users.to_dict("records")
                if u["role"] in allowed and u["username"] != g.user["username"]
                and (u["role"] != "developer" or g.user["role"] == "developer")]
    return render_template("users.html", users=users.to_dict("records"), allowed=allowed,
                           editable=editable, mail_ok=mailer.configured())


def _manageable_target(username):
    users = auth.load_users()
    row = users[users["username"] == username]
    if row.empty:
        abort(404)
    role = row.iloc[0]["role"]
    if (username == g.user["username"] or role not in auth.manageable_roles(g.user["role"])
            or (role == "developer" and g.user["role"] != "developer")):
        abort(403)
    return users


@app.post("/users/new")
@login_required("admin")
def user_new():
    role = request.form.get("role", "")
    if role not in auth.manageable_roles(g.user["role"]):
        abort(403)
    username = request.form.get("username", "")
    email = request.form.get("email", "").strip()
    pw = request.form.get("password", "")
    name = request.form.get("full_name", "")
    err = auth.add_user(username, name, role, pw, email)
    if err:
        flash(err, "error")
    elif request.form.get("send_email") and email:
        html, text = emailkit.render(
            "account", "Welcome to TGM Assets", f"Hello {name or username}, an account was created for you.",
            facts=[("Username", username.strip().lower()), ("Temporary password", pw),
                   ("Role", auth.ROLE_LABEL[role])],
            button=("Sign in", "/login"),
            note="Please sign in and change your password (or use 'Forgot password?' on the sign-in page).")
        mail_err = mailer.send_email(email, "Your TGM Assets account", html, text=text)
        flash(mail_err or "User created and email sent.", "error" if mail_err else "success")
    else:
        flash("User created.", "success")
    return redirect(url_for("users_page"))


@app.post("/users/<username>/role")
@login_required("admin")
def user_role(username):
    users = _manageable_target(username)
    role = request.form.get("role", "")
    if role not in auth.manageable_roles(g.user["role"]):
        abort(403)
    users.loc[users["username"] == username, "role"] = role
    auth.save_users(users)
    flash("Role updated.", "success")
    return redirect(url_for("users_page"))


@app.post("/users/<username>/email")
@login_required("admin")
def user_email(username):
    _manageable_target(username)
    err = auth.set_email(username, request.form.get("email", ""))
    flash(err or "Email updated.", "error" if err else "success")
    return redirect(url_for("users_page"))


@app.post("/users/<username>/password")
@login_required("admin")
def user_password(username):
    _manageable_target(username)
    err = auth.set_password(username, request.form.get("password", ""))
    flash(err or "Password updated.", "error" if err else "success")
    return redirect(url_for("users_page"))


@app.post("/users/<username>/delete")
@login_required("admin")
def user_delete(username):
    _manageable_target(username)
    err = auth.delete_user(username)
    flash(err or "User deleted.", "error" if err else "success")
    return redirect(url_for("users_page"))


@app.post("/users/email")
@login_required("admin")
def user_compose():
    users = auth.load_users().set_index("username")
    subject = request.form.get("subject", "").strip()
    body = request.form.get("body", "").strip()
    targets = [u for u in request.form.getlist("to") if u in users.index and users.at[u, "email"]]
    if not (targets and subject and body):
        flash("Choose recipients and enter a subject and message.", "error")
        return redirect(url_for("users_page"))
    sender = g.user["full_name"] or g.user["username"]
    html, text = emailkit.render("account", subject, body, note=f"Sent by {sender}",
                                 button=("Open TGM Assets", "/"))
    errs = [f"{u}: {e}" for u in targets
            if (e := mailer.send_email(users.at[u, "email"], subject, html, text=text))]
    flash("; ".join(errs) if errs else f"Sent to {len(targets)} user(s).", "error" if errs else "success")
    return redirect(url_for("users_page"))


@app.route("/profile", methods=["GET", "POST"])
@login_required()
def profile():
    if request.method == "POST":
        if request.form.get("form") == "email":
            err = auth.set_email(g.user["username"], request.form.get("email", ""))
            flash(err or "Email saved.", "error" if err else "success")
        else:
            err = auth.set_password(g.user["username"], request.form.get("password", ""))
            flash(err or "Password updated.", "error" if err else "success")
        return redirect(url_for("profile"))
    items = notify.for_user(g.user["username"])
    return render_template("profile.html", notification_count=len(items),
                           latest_notification=items.iloc[0]["created"] if not items.empty else None)


# ---------- settings (developer): logo and SMTP ----------
LOGO_TYPES = {b"\x89PNG\r\n\x1a\n": ("png", "image/png"), b"\xff\xd8\xff": ("jpg", "image/jpeg"),
              b"RIFF": ("webp", "image/webp")}


def _custom_logo():
    for ext, mime in (("png", "image/png"), ("jpg", "image/jpeg"), ("webp", "image/webp")):
        p = store.DATA / f"logo.{ext}"
        if p.exists():
            return p, mime
    return None, None


@app.route("/logo")
def logo():
    path, mime = _custom_logo()
    resp = send_file(path, mimetype=mime) if path else send_file(BASE / "static" / "logo.svg",
                                                                  mimetype="image/svg+xml")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/settings")
@login_required("developer")
def settings():
    return render_template("settings.html", smtp=mailer.current(), env=mailer.env_overrides(),
                           mail_ok=mailer.configured(), resend=mailer.resend_current(),
                           custom_logo=_custom_logo()[0] is not None)


@app.post("/settings/smtp")
@login_required("developer")
def settings_smtp():
    f = request.form
    err = mailer.save_config(f.get("host", ""), f.get("port", ""), f.get("user", ""),
                             f.get("password", ""), f.get("sender", ""))
    flash(err or "SMTP settings saved.", "error" if err else "success")
    return redirect(url_for("settings"))


@app.post("/settings/smtp/test")
@login_required("developer")
def settings_smtp_test():
    to = g.user["email"] or request.form.get("to", "").strip()
    if not auth.EMAIL_RE.match(to):
        flash("Add your email on My Profile, or enter an address to send the test to.", "error")
    else:
        html, text = emailkit.render("info", "Your email settings work",
                                     "This is a test email from TGM Assets.",
                                     button=("Open settings", "/settings"))
        err = mailer.send_email(to, "TGM Assets test email", html, text=text)
        flash(err or f"Test email sent to {to}.", "error" if err else "success")
    return redirect(url_for("settings"))


@app.post("/settings/logo")
@login_required("developer")
def settings_logo():
    if request.form.get("remove"):
        for ext in ("png", "jpg", "webp"):
            (store.DATA / f"logo.{ext}").unlink(missing_ok=True)
        flash("Custom logo removed. Using the default logo.", "success")
        return redirect(url_for("settings"))
    file = request.files.get("logo")
    data = file.read(1024 * 1024 + 1) if file else b""
    kind = next((v for magic, v in LOGO_TYPES.items() if data.startswith(magic)), None)
    if kind and kind[0] == "webp" and data[8:12] != b"WEBP":
        kind = None
    if not kind or len(data) > 1024 * 1024:
        flash("Upload a PNG, JPG or WebP image under 1 MB.", "error")
        return redirect(url_for("settings"))
    for ext in ("png", "jpg", "webp"):
        (store.DATA / f"logo.{ext}").unlink(missing_ok=True)
    (store.DATA / f"logo.{kind[0]}").write_bytes(data)
    flash("Logo updated.", "success")
    return redirect(url_for("settings"))


# ---------- reports / sample pages ----------
EXPORTS = {"assets": (store.load_assets, "assets.csv"),
           "work_orders": (store.load_work_orders, "work_orders.csv"),
           "procurement": (store.load_procurement, "procurement.csv")}


@app.route("/reports")
@login_required("admin")
def reports():
    return render_template("reports.html", exports=list(EXPORTS))


@app.route("/reports/<name>.csv")
@login_required("admin")
def report_csv(name):
    if name not in EXPORTS:
        abort(404)
    load, filename = EXPORTS[name]
    return Response(load().to_csv(index=False), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.route("/inventory")
@login_required()
def inventory():
    items = [dict(item="Bearings", stock=80, minimum=100), dict(item="Filters", stock=250, minimum=100),
             dict(item="Belts", stock=25, minimum=50)]
    return render_template("sample.html", title="Inventory", items=items, kind="inventory")


@app.route("/vendors")
@login_required()
def vendors():
    items = [dict(name="ABC Industrial", score=92), dict(name="Global Parts", score=84),
             dict(name="Prime Supply", score=78)]
    return render_template("sample.html", title="Vendors", items=items, kind="vendors")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
