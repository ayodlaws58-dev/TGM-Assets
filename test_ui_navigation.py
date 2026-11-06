import re

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
        {"username": role, "full_name": role, "role": role}
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
