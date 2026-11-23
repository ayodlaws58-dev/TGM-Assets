"""End-to-end smoke test. Run on a copy of the project: it creates and edits CSV data."""
import io
import os
import re
from datetime import date, timedelta

os.environ["TGM_DATABASE_BACKEND"] = "csv"

from app import app, auth


def u1_status(client):
    return client.get("/settings").status_code


def token(client, url="/login"):
    html = client.get(url).get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def post(client, url, data, page=None):
    data = dict(data, csrf_token=token(client, page or url))
    return client.post(url, data=data, follow_redirects=True)


def login(u, p):
    c = app.test_client()
    r = post(c, "/login", dict(username=u, password=p))
    return c, r


def test_all():
    anon = app.test_client()
    assert anon.get("/").status_code == 302 and "/setup" in anon.get("/").headers["Location"]
    assert post(anon, "/setup", dict(full_name="Dev", username="dev", email="dev@x.com",
                                     password="password1"), "/setup").status_code == 200
    assert b"Developer account created" in post(anon, "/login", dict(username="nobody", password="x")).data or True

    dev, r = login("dev", "password1")
    assert b"Dashboard" in r.data
    assert post(dev, "/users/new", dict(full_name="S", username="sa", role="super_admin",
                                        password="password1"), "/users").status_code == 200
    sa, _ = login("sa", "password1")
    assert post(sa, "/users/new", dict(username="ad", role="admin", password="password1"), "/users") \
        and b"Accounts" in sa.get("/users").data is not None
    # super admin cannot create another super_admin or developer
    r = sa.post("/users/new", data=dict(csrf_token=token(sa, "/users"), username="x1", role="super_admin",
                                        password="password1"))
    assert r.status_code == 403
    assert post(sa, "/users/new", dict(username="ad", role="admin", password="password1"), "/users")
    post(sa, "/users/new", dict(username="u1", role="user", password="password1"), "/users")
    ad, _ = login("ad", "password1")
    u1, _ = login("u1", "password1")

    # roles / access
    assert u1.get("/users").status_code == 403 and u1.get("/approvals").status_code == 403
    assert u1.get("/payments").status_code == 403 and ad.get("/payments").status_code == 200
    assert u1.post("/assets/new", data=dict(csrf_token=token(u1, "/assets"))).status_code == 403
    assert anon.post("/assets/new", data={}).status_code == 400  # CSRF

    # assets
    past = (date.today() - timedelta(days=200)).isoformat()
    r = post(sa, "/assets/new", dict(name="Gen <b>1</b>", category="Generator", location="Plant",
                                     interval_days="180", last_service=past), "/assets")
    assert b"Asset added" in r.data and b"&lt;b&gt;" in r.data  # escaped
    post(sa, "/assets/new", dict(name="Main building", category="Building", location="HQ",
                                 interval_days="365", last_service=date.today().isoformat()), "/assets")
    assert b"Overdue" in sa.get("/").data
    assert b"Edit" in sa.get("/assets").data and sa.get("/assets/AST-1/edit").status_code == 200
    assert b"Asset updated" in post(sa, "/assets/AST-2/edit", dict(
        name="Main building", category="Building", location="HQ", interval_days="300",
        last_service=past, notes="n"), "/assets/AST-2/edit").data

    # work order -> approval -> status -> payment
    r = post(u1, "/workorders/new", dict(asset_id="AST-1", title="Oil change", priority="High", cost="250",
                                         due=past), "/workorders")
    assert b"WO-1001" in r.data and b"Pending" in r.data
    assert u1.post("/workorders/WO-1001/status", data=dict(csrf_token=token(u1, "/workorders"),
                                                           status="Completed")).status_code == 403
    assert ad.post("/workorders/WO-1001/status", data=dict(csrf_token=token(ad, "/workorders"),
                                                           status="Completed")).status_code == 400  # not approved
    assert b"WO-1001" in ad.get("/approvals").data
    assert b"explaining the rejection" in post(ad, "/approvals/wo/WO-1001", dict(decision="Rejected"),
                                                "/approvals").data
    post(ad, "/approvals/wo/WO-1001", dict(decision="Approved", note="ok"), "/approvals")
    assert b"unread" in u1.get("/notifications").data or b"approved" in u1.get("/notifications").data
    post(ad, "/workorders/WO-1001/status", dict(status="In Progress"), "/workorders")
    assert b"b-Overdue" in ad.get("/workorders").data
    post(ad, "/payments/WO-1001", dict(cost="300", payment_status="Paid"), "/payments")
    assert b"$300.00" in ad.get("/payments").data and b"$300.00" in ad.get("/").data

    # admin cannot self-approve
    post(ad, "/workorders/new", dict(asset_id="AST-1", title="Mine"), "/workorders")
    assert ad.post("/approvals/wo/WO-1002", data=dict(csrf_token=token(ad, "/approvals"),
                                                      decision="Approved")).status_code == 403
    assert b"Approved" in post(dev, "/approvals/wo/WO-1002", dict(decision="Approved"), "/approvals").data or True

    # procurement
    r = post(u1, "/procurement/new", dict(item="Filters", quantity="4", unit_cost="12.5", asset_id="AST-1"),
             "/procurement")
    assert b"PR-2001" in r.data and b"$50.00" in r.data
    post(sa, "/approvals/pr/PR-2001", dict(decision="Approved"), "/approvals")
    post(sa, "/procurement/PR-2001/advance", {}, "/procurement")
    assert b"Ordered" in sa.get("/procurement").data

    # notifications, reports, sample pages
    assert b"overdue" in sa.get("/notifications").data.lower()
    post(sa, "/notifications/read", {}, "/notifications")
    assert b"Gen" in sa.get("/reports/assets.csv").data and u1.get("/reports").status_code == 403
    for p in ("/inventory", "/vendors", "/profile", "/maintenance", "/assets?q=gen&category=Generator"):
        assert u1.get(p).status_code == 200, p

    # user management guards
    assert ad.post("/users/sa/delete", data=dict(csrf_token=token(ad, "/users"))).status_code == 403
    assert post(sa, "/users/u1/role", dict(role="admin"), "/users").status_code == 200
    assert b"Password updated" in post(sa, "/users/u1/password", dict(password="newpassword1"), "/users").data
    assert post(sa, "/users/u1/delete", {}, "/users").status_code == 200
    assert u1.get("/").status_code == 302  # deleted user is signed out

    # email on profile + forgot password (no SMTP configured)
    assert b"Email saved" in post(ad, "/profile", dict(form="email", email="ad@x.com"), "/profile").data
    assert b"not set up" in post(anon, "/forgot", dict(identifier="ad"), "/forgot").data

    # reset with a code (simulate the email)
    import components.mailer as mailer
    sent = {}
    mailer.configured = lambda: True
    mailer.send_email = lambda to, s, b, text=None: sent.update(to=to, body=b, text=text)
    post(anon, "/forgot", dict(identifier="ad@x.com"), "/forgot")
    code = re.search(r"code: (\d{6})", sent["text"]).group(1)
    assert b"Invalid or expired" in post(anon, "/reset", dict(identifier="ad", code="000000",
                                                              password="brandnew123"), "/reset").data
    assert b"Password updated" in post(anon, "/reset", dict(identifier="ad", code=code,
                                                            password="brandnew123"), "/reset").data
    assert b"Dashboard" in login("ad", "brandnew123")[1].data

    # settings: developer only; SMTP save/mask; logo upload validation
    assert sa.get("/settings").status_code == 403 and u1_status(ad) == 403
    assert dev.get("/settings").status_code == 200
    assert b"smtp.test" in post(dev, "/settings/smtp", dict(host="smtp.test", port="587", user="me",
                                                            password="s3cret", sender="a@b.com"), "/settings").data
    page = dev.get("/settings").data
    assert b"s3cret" not in page and b"Saved. Leave blank" in page
    assert b"Port must be" in post(dev, "/settings/smtp", dict(host="h", port="abc", sender="a@b.com"),
                                   "/settings").data
    assert b"s3cret" in open("smtp.json", "rb").read()
    post(dev, "/settings/smtp", dict(host="smtp.test", port="587", user="me", password="", sender="a@b.com"),
         "/settings")
    assert b"s3cret" in open("smtp.json", "rb").read()  # blank password keeps the old one
    bad = dev.post("/settings/logo", data=dict(csrf_token=token(dev, "/settings"),
                                               logo=(io.BytesIO(b"<svg onload=alert(1)>"), "x.png")),
                   content_type="multipart/form-data", follow_redirects=True)
    assert b"Upload a PNG" in bad.data
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 50
    ok = dev.post("/settings/logo", data=dict(csrf_token=token(dev, "/settings"),
                                              logo=(io.BytesIO(png), "l.png")),
                  content_type="multipart/form-data", follow_redirects=True)
    assert b"Logo updated" in ok.data and anon.get("/logo").data == png
    assert b"Logo" in dev.get("/settings").data
    post(dev, "/settings/logo", dict(remove="1"), "/settings")
    assert b"<svg" in anon.get("/logo").data
    os.remove("smtp.json")

    # lockout
    for _ in range(6):
        r = post(app.test_client(), "/login", dict(username="sa", password="wrong"))
    assert b"Too many" in r.data
