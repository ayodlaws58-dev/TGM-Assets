import io
import re
from datetime import date

import pandas as pd
import pytest

import app as app_module
from components import auth, notify, store


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TGM_DATABASE_BACKEND", "csv")
    monkeypatch.setattr(store, "DATA", tmp_path)
    monkeypatch.setattr(auth, "USERS_FILE", tmp_path / "users.csv")
    monkeypatch.setattr(notify, "FILE", tmp_path / "notifications.csv")
    monkeypatch.setattr(app_module, "maybe_run_reminders", lambda: None)
    monkeypatch.setattr(notify.mailer, "configured", lambda: False)
    users = [
        {"username": role, "full_name": role, "role": role, "email": f"{role}@example.com"}
        for role in ("user", "admin", "developer")
    ]
    pd.DataFrame(users).reindex(columns=auth.COLS, fill_value="").fillna("").to_csv(auth.USERS_FILE, index=False)
    pd.DataFrame([{
        "asset_id": "AST-1", "name": "Generator", "category": "Generator",
        "location": "Plant", "last_service": "2025-01-01",
        "interval_days": "180", "notes": "",
    }]).reindex(columns=store.ASSET_COLS).to_csv(tmp_path / "assets.csv", index=False)
    return app_module.app.test_client()


def sign_in(client, role):
    with client.session_transaction() as session:
        session["username"] = role


def group_tag(html, name):
    match = re.search(
        r'(<details class="nav-group"[^>]*>)\s*<summary>' + name + r"</summary>",
        html,
    )
    assert match is not None
    return match.group(1)


@pytest.mark.parametrize("role", ["user", "admin", "developer"])
def test_dashboard_keeps_details_collapsed_and_available(client, role):
    sign_in(client, role)
    response = client.get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Needs attention" in html
    assert html.count('<details class="card dashboard-details">') == 3
    for label in ("Total work-order cost", "Work orders by status",
                  "Servicing due", "Repainting due"):
        assert label in html
    assert ('Pending approvals' in html) == (role != "user")


@pytest.mark.parametrize("role", ["user", "admin", "developer"])
def test_sidebar_groups_and_role_access(client, role):
    sign_in(client, role)
    html = client.get("/assets").get_data(as_text=True)
    assert " open" in group_tag(html, "Operations")
    assert " open" not in group_tag(html, "Purchasing")
    assert 'aria-current="page">Assets</a>' in html
    assert ("<summary>Administration</summary>" in html) == (role != "user")
    assert ('href="/settings"' in html) == (role == "developer")


def test_sidebar_opens_for_nested_asset_page_and_admin_page(client):
    sign_in(client, "developer")
    html = client.get("/assets/AST-1/edit").get_data(as_text=True)
    assert " open" in group_tag(html, "Operations")
    assert 'aria-current="page">Assets</a>' in html
    html = client.get("/approvals").get_data(as_text=True)
    assert " open" in group_tag(html, "Administration")
    assert 'aria-current="page">Approvals</a>' in html


def test_notification_details_and_profile_are_scoped_to_user(client):
    notify.send(["user"], "reminder", "Service due", "Check <generator> soon")
    notify.send(["admin"], "info", "Private update", "Only for admin")
    sign_in(client, "user")

    html = client.get("/notifications").get_data(as_text=True)
    assert '<details class="notification-details">' in html
    assert "More details" in html
    assert "notification-tooltip" in html
    assert "Check &lt;generator&gt; soon" in html
    assert "Private update" not in html
    assert "Mark as read" in html

    html = client.get("/profile").get_data(as_text=True)
    assert "Account details" in html
    assert "View notifications" in html
    assert re.search(r"<dt>Unread</dt><dd>1</dd>", html)
    assert re.search(r"<dt>Total</dt><dd>1</dd>", html)
    assert "Only for admin" not in html


def test_opening_notification_does_not_mark_it_read_and_read_is_scoped(client):
    notify.send(["user"], "info", "For user", "First")
    notify.send(["admin"], "info", "For admin", "Second")
    ids = notify._load().set_index("username")["notif_id"]
    sign_in(client, "user")
    client.get("/notifications")
    assert notify.unread_count("user") == 1
    with client.session_transaction() as session:
        token = session["csrf"]

    response = client.post("/notifications/read", data={"csrf_token": token, "id": ids["admin"]})
    assert response.status_code == 302
    assert notify.unread_count("admin") == 1
    assert notify.unread_count("user") == 1
    client.post("/notifications/read", data={"csrf_token": token, "id": ids["user"]})
    assert notify.unread_count("user") == 0
    assert notify.unread_count("admin") == 1


def test_asset_details_include_depreciation_children_and_qr_label(client):
    sign_in(client, "developer")
    assets = store.load_assets()
    assets.loc[assets["asset_id"] == "AST-1", ["purchase_date", "cost", "useful_life_years",
                                                   "salvage_value"]] = [
        "2026-01-01", "10000", "10", "1000"]
    child = {column: "" for column in store.ASSET_COLS}
    child.update(asset_id="AST-2", name="Generator part", category="Other", parent_id="AST-1",
                 status="Active", last_service="2026-01-01", interval_days="180", attachments="[]")
    store.save_assets(store.add_row(assets, **child))

    response = client.get("/assets/AST-1")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert b"Current book value" in response.data
    record = store.load_assets().iloc[0].to_dict()
    assert app_module._asset_depreciation(record, date.fromisoformat("2026-01-01"))["book_value"] == 10000
    assert b"Generator part" in response.data
    assert b"Print QR label" in response.data
    qr = client.get("/assets/AST-1/qr.svg")
    assert qr.status_code == 200 and b"<svg" in qr.data
    assert b"AST-1" in client.get("/assets/AST-1/label").data


def test_asset_upload_pdf_and_reject_invalid_file(client):
    sign_in(client, "developer")
    response = client.get("/assets/AST-1")
    assert response.status_code == 200
    with client.session_transaction() as session:
        csrf = session["csrf"]

    uploaded = client.post("/assets/AST-1/attachments", data={
        "csrf_token": csrf, "files": (io.BytesIO(b"%PDF-1.7\nasset manual"), "manual.pdf")
    }, content_type="multipart/form-data", follow_redirects=True)
    assert b"Uploaded 1 attachment" in uploaded.data
    attachment = store.load_assets().iloc[0]["attachments"]
    import json
    item = json.loads(attachment)[0]
    downloaded = client.get(f"/assets/AST-1/attachments/{item['id']}")
    assert downloaded.status_code == 200 and downloaded.data.startswith(b"%PDF-")
    assert b"manual.pdf" in client.get("/assets/AST-1").data

    invalid = client.post("/assets/AST-1/attachments", data={
        "csrf_token": csrf, "files": (io.BytesIO(b"not a pdf"), "bad.pdf")
    }, content_type="multipart/form-data", follow_redirects=True)
    assert b"Uploads must be valid" in invalid.data


def test_asset_parent_cycle_and_retirement_validation(client):
    sign_in(client, "developer")
    assets = store.load_assets()
    child = {column: "" for column in store.ASSET_COLS}
    child.update(asset_id="AST-2", name="Child", category="Other", parent_id="AST-1",
                 status="Active", last_service="2026-01-01", interval_days="180", attachments="[]")
    store.save_assets(store.add_row(assets, **child))
    client.get("/assets/AST-1/edit")
    with client.session_transaction() as session:
        csrf = session["csrf"]
    response = client.post("/assets/AST-1/edit", data={
        "csrf_token": csrf, "name": "Generator", "category": "Generator", "parent_id": "AST-2",
        "status": "Active", "last_service": "2025-01-01", "interval_days": "180"
    }, follow_redirects=True)
    assert b"Check the asset details" in response.data
    assert store.load_assets().set_index("asset_id").at["AST-1", "parent_id"] == ""

    response = client.post("/assets/AST-1/edit", data={
        "csrf_token": csrf, "name": "Generator", "category": "Generator", "status": "Retired",
        "last_service": "2025-01-01", "interval_days": "180"
    }, follow_redirects=True)
    assert b"Check the asset details" in response.data
    response = client.post("/assets/AST-1/edit", data={
        "csrf_token": csrf, "name": "Generator", "category": "Generator", "status": "Retired",
        "last_service": "2025-01-01", "interval_days": "180", "retired_on": date.today().isoformat()
    }, follow_redirects=True)
    assert b"Asset updated" in response.data
    assert store.with_due_dates(store.load_assets()).set_index("asset_id").at["AST-1", "status"] == "No schedule"
    previous_count = len(store.load_work_orders())
    response = client.post("/workorders/new", data={
        "csrf_token": csrf, "asset_id": "AST-1", "title": "Work on retired asset"
    }, follow_redirects=True)
    assert b"Work orders cannot be created" in response.data
    assert len(store.load_work_orders()) == previous_count


def test_calendar_schedule_intake_and_completed_work_order_history(client):
    sign_in(client, "admin")
    client.get("/maintenance")
    with client.session_transaction() as session:
        csrf = session["csrf"]

    response = client.post("/maintenance/schedules/new", data={
        "csrf_token": csrf, "asset_id": "AST-1", "title": "Annual repaint",
        "interval_value": "1", "interval_unit": "years",
        "last_completed": "2024-06-01", "assigned_to": "admin"
    }, follow_redirects=True)
    assert b"Preventive schedule created" in response.data
    schedules = store.load_schedules()
    due = store.schedule_rows(schedules, store.load_assets(), today="2025-05-01")
    assert due[0]["next_due"].strftime("%Y-%m-%d") == "2025-06-01"
    assert b"Annual repaint" in client.get("/maintenance").data

    count = len(schedules)
    response = client.post("/maintenance/schedules/new", data={
        "csrf_token": csrf, "asset_id": "AST-1", "title": "Unsupported usage schedule",
        "interval_value": "10", "interval_unit": "hours"
    }, follow_redirects=True)
    assert b"valid calendar interval" in response.data
    assert len(store.load_schedules()) == count

    sign_in(client, "user")
    client.get("/workorders")
    with client.session_transaction() as session:
        csrf = session["csrf"]
    response = client.post("/workorders/new", data={
        "csrf_token": csrf, "asset_id": "AST-1", "title": "Repaint generator housing",
        "priority": "High", "assigned_to": "admin", "schedule_id": schedules.iloc[0]["schedule_id"],
        "description": "Paint is peeling."
    }, follow_redirects=True)
    assert b"Work order submitted" in response.data
    orders = store.load_work_orders()
    orders.loc[orders["wo_id"] == "WO-1001", "approval_status"] = "Approved"
    store.save_work_orders(orders)

    sign_in(client, "admin")
    response = client.post("/workorders/WO-1001/status", data={
        "csrf_token": csrf, "status": "Completed", "completion_notes": "Repainted and inspected.",
        "actual_cost": "125.50", "downtime_hours": "4.25"
    }, follow_redirects=True)
    assert response.status_code == 200
    history = store.load_service_history()
    assert len(history) == 1
    record = history.iloc[0]
    assert record["work_order_id"] == "WO-1001"
    assert record["completion_notes"] == "Repainted and inspected."
    assert record["cost"] == "125.50" and record["downtime_hours"] == "4.25"
    completed_order = store.load_work_orders().set_index("wo_id").loc["WO-1001"]
    assert completed_order["actual_cost"] == "125.50" and completed_order["cost"] == "125.50"
    assert store.load_schedules().iloc[0]["last_completed"] == date.today().isoformat()
    detail = client.get("/assets/AST-1")
    assert b"Repainted and inspected." in detail.data and b"4.25 h" in detail.data
    order = {column: "" for column in store.WO_COLS}
    order.update(wo_id="WO-1002", asset_id="AST-1", title="Inspect repaint",
                 status="In Progress", approval_status="Approved", cost="50",
                 payment_status="Unpaid")
    store.save_work_orders(store.add_row(store.load_work_orders(), **order))
    with client.session_transaction() as session:
        csrf = session["csrf"]
    response = client.post("/workorders/WO-1002/status", data={
        "csrf_token": csrf, "status": "Completed", "completion_notes": "",
        "actual_cost": "125.50", "downtime_hours": "4.25"
    })
    assert response.status_code == 400
    assert len(store.load_service_history()) == 1
    assert store.load_work_orders().set_index("wo_id").at["WO-1002", "status"] == "In Progress"


def test_calendar_recurrence_handles_month_end_and_leap_day():
    assert store.add_calendar_interval("2024-01-31", 1, "months").strftime("%Y-%m-%d") == "2024-02-29"
    assert store.add_calendar_interval("2024-02-29", 1, "years").strftime("%Y-%m-%d") == "2025-02-28"


def test_preventive_schedule_reminder_emails_are_deduplicated(client, monkeypatch):
    schedules = store.load_schedules()
    schedules = store.add_row(
        schedules, schedule_id="SCH-3001", asset_id="AST-1", title="Annual repaint",
        interval_value="1", interval_unit="years", last_completed="2020-01-01",
        active="1", assigned_to="admin")
    store.save_schedules(schedules)
    sent = []
    monkeypatch.setattr(notify.mailer, "configured", lambda: True)
    monkeypatch.setattr(notify.mailer, "send_email", lambda *args: sent.append(args))

    notify.run_reminders()
    first_count = len(sent)
    notify.run_reminders()
    assert first_count > 0
    assert len(sent) == first_count
    assert any("Preventive maintenance overdue" in subject for _, subject, _ in sent)


def test_resend_api_is_used_for_configured_reminders(monkeypatch):
    import json

    from components import mailer

    monkeypatch.setenv("RESEND_API_KEY", "test-token")
    monkeypatch.setenv("RESEND_FROM_EMAIL", "TGM Assets <assets@example.com>")
    monkeypatch.setattr(mailer, "_config", lambda: dict.fromkeys(mailer.KEYS, ""))
    requests = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def fake_urlopen(req, timeout):
        requests.append((req, timeout))
        return Response()

    monkeypatch.setattr(mailer, "urlopen", fake_urlopen)
    assert mailer.configured() and mailer.using_resend()
    assert mailer.send_email("admin@example.com", "Maintenance due", "Asset overdue.") is None
    req, timeout = requests[0]
    assert req.full_url == "https://api.resend.com/emails"
    assert json.loads(req.data)["to"] == ["admin@example.com"]
    assert timeout == 15


def test_failed_reminder_email_is_retried_without_duplicate_notification(client, monkeypatch):
    results = iter(["temporary provider error", None])
    attempts = []
    monkeypatch.setattr(notify.mailer, "configured", lambda: True)
    monkeypatch.setattr(notify.mailer, "send_email",
                        lambda *args: (attempts.append(args), next(results))[1])

    notify.send(["admin"], "overdue", "Schedule overdue", "Asset due.",
                key="schedule-overdue:SCH-1:2026-01-01")
    notify.send(["admin"], "overdue", "Schedule overdue", "Asset due.",
                key="schedule-overdue:SCH-1:2026-01-01")

    messages = notify._load()
    assert len(messages) == 1
    assert messages.iloc[0]["email_sent"] == "1"
    assert len(attempts) == 2


def test_profile_email_and_password_forms_remain_functional(client):
    sign_in(client, "user")
    client.get("/profile")
    with client.session_transaction() as session:
        token = session["csrf"]
    response = client.post("/profile", data={"csrf_token": token, "form": "email",
                                              "email": "user@example.com"}, follow_redirects=True)
    assert b"Email saved." in response.data
    assert auth.load_users().set_index("username").at["user", "email"] == "user@example.com"
    response = client.post("/profile", data={"csrf_token": token, "form": "password",
                                              "password": "newpassword"}, follow_redirects=True)
    assert b"Password updated." in response.data
    assert auth.verify("user", "newpassword") is not None
