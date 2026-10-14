import time

import pytest
from werkzeug.security import check_password_hash

from app import create_app
from app.db import get_db
from conftest import PASSWORD, get_token, login


# ---------- login ----------

@pytest.mark.parametrize("email,dashboard", [
    ("admin@x.com", "/admin/"),
    ("warden@x.com", "/warden/"),
    ("s1@x.com", "/student/"),
])
def test_valid_login_goes_to_own_dashboard(client, users, email, dashboard):
    resp = login(client, email)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(dashboard)
    assert client.get(dashboard).status_code == 200


def test_email_is_case_insensitive(client, users):
    assert login(client, "ADMIN@X.COM").status_code == 302


def test_wrong_password_fails(client, users):
    resp = login(client, "admin@x.com", "wrong-password")
    assert resp.status_code == 401
    assert b"Incorrect email or password" in resp.data
    assert client.get("/admin/").status_code == 302  # still not logged in


def test_unknown_email_gives_same_message(client, users):
    resp = login(client, "nobody@x.com")
    assert resp.status_code == 401
    assert b"Incorrect email or password" in resp.data


def test_empty_fields_rejected(client, users):
    resp = login(client, "", "")
    assert resp.status_code == 400


def test_deactivated_account_cannot_log_in(app, client, users):
    with app.app_context():
        db = get_db()
        db.execute("UPDATE student SET is_active = 0 WHERE email = 's1@x.com'")
        db.commit()
    assert login(client, "s1@x.com").status_code == 401


def test_deactivation_logs_out_existing_session(app, client, users):
    login(client, "s1@x.com")
    assert client.get("/student/").status_code == 200
    with app.app_context():
        db = get_db()
        db.execute("UPDATE student SET is_active = 0 WHERE email = 's1@x.com'")
        db.commit()
    assert client.get("/student/").status_code == 302


def test_logout_ends_session(client, users):
    login(client, "admin@x.com")
    client.get("/admin/")  # rendering a page creates the new session's CSRF token
    with client.session_transaction() as s:
        token = s["_csrf"]
    resp = client.post("/logout", data={"csrf_token": token})
    assert resp.status_code == 302
    assert client.get("/admin/").status_code == 302


def test_logout_requires_post(client, users):
    login(client, "admin@x.com")
    assert client.get("/logout").status_code == 405


def test_logged_in_user_skips_login_page(client, users):
    login(client, "admin@x.com")
    resp = client.get("/login")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/admin/")


# ---------- access control ----------

@pytest.mark.parametrize("path", ["/admin/", "/warden/", "/student/"])
def test_anonymous_redirected_to_login(client, users, path):
    resp = client.get(path)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


@pytest.mark.parametrize("email,forbidden", [
    ("s1@x.com", ["/admin/", "/warden/"]),
    ("warden@x.com", ["/admin/", "/student/"]),
    ("admin@x.com", ["/warden/", "/student/"]),
])
def test_wrong_role_gets_403(client, users, email, forbidden):
    login(client, email)
    for path in forbidden:
        assert client.get(path).status_code == 403


def test_forged_role_in_form_is_ignored(client, users):
    token = get_token(client)
    resp = client.post("/login", data={
        "email": "s1@x.com", "password": PASSWORD, "role": "admin", "csrf_token": token})
    assert resp.headers["Location"].endswith("/student/")
    assert client.get("/admin/").status_code == 403


# ---------- CSRF ----------

def test_login_without_csrf_token_rejected(client, users):
    client.get("/login")
    resp = client.post("/login", data={"email": "admin@x.com", "password": PASSWORD})
    assert resp.status_code == 400


def test_login_with_wrong_csrf_token_rejected(client, users):
    client.get("/login")
    resp = client.post("/login", data={"email": "admin@x.com", "password": PASSWORD, "csrf_token": "bad"})
    assert resp.status_code == 400


def test_logout_without_csrf_token_rejected(client, users):
    login(client, "admin@x.com")
    assert client.post("/logout").status_code == 400
    assert client.get("/admin/").status_code == 200  # still logged in


# ---------- lockout ----------

def test_lockout_after_repeated_failures(client, users):
    for _ in range(5):
        assert login(client, "admin@x.com", "bad").status_code == 401
    resp = login(client, "admin@x.com", PASSWORD)  # even the right password is refused now
    assert resp.status_code == 429


def test_lockout_is_per_email(client, users):
    for _ in range(5):
        login(client, "admin@x.com", "bad")
    assert login(client, "warden@x.com").status_code == 302


def test_successful_login_clears_failures(client, users):
    for _ in range(3):
        login(client, "admin@x.com", "bad")
    assert login(client, "admin@x.com").status_code == 302


# ---------- sessions and passwords ----------

def test_idle_session_expires(tmp_path, users):
    app = create_app({"TESTING": True, "SECRET_KEY": "k", "DATABASE_PATH": str(tmp_path / "idle.db"),
                      "PERMANENT_SESSION_LIFETIME": 1})
    # build a user in this separate app
    from werkzeug.security import generate_password_hash
    with app.app_context():
        db = get_db()
        db.execute("INSERT INTO admin (full_name,email,phone,password_hash) VALUES ('A','a@x.com','1',?)",
                   (generate_password_hash(PASSWORD),))
        db.commit()
    c = app.test_client()
    login(c, "a@x.com")
    assert c.get("/admin/").status_code == 200
    time.sleep(2)
    assert c.get("/admin/").status_code == 302


def test_session_cookie_flags(client, users):
    resp = login(client, "admin@x.com")
    cookie = resp.headers["Set-Cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie


def test_passwords_are_stored_hashed(app, users):
    with app.app_context():
        h = get_db().execute("SELECT password_hash FROM admin").fetchone()[0]
    assert h != PASSWORD and check_password_hash(h, PASSWORD)


def test_dashboard_never_shows_password_hash(client, users):
    login(client, "admin@x.com")
    assert b"scrypt" not in client.get("/admin/").data and b"pbkdf2" not in client.get("/admin/").data


# ---------- errors, startup, create-admin ----------

def test_404_is_friendly(client):
    resp = client.get("/no-such-page")
    assert resp.status_code == 404 and b"Traceback" not in resp.data


def test_refuses_default_secret_key_in_production(tmp_path):
    with pytest.raises(RuntimeError):
        create_app({"SECRET_KEY": "change-me", "TESTING": False, "DEBUG": False,
                    "DATABASE_PATH": str(tmp_path / "x.db")})


def test_create_admin_command(app):
    runner = app.test_cli_runner()
    result = runner.invoke(args=["create-admin", "--name", "Boss", "--email", "Boss@X.com",
                                 "--phone", "123", "--password", "longenough1"])
    assert result.exit_code == 0, result.output
    with app.app_context():
        row = get_db().execute("SELECT email, password_hash FROM admin").fetchone()
    assert row["email"] == "boss@x.com"
    assert row["password_hash"] != "longenough1"


def test_create_admin_rejects_short_password_and_duplicates(app, users):
    runner = app.test_cli_runner()
    short = runner.invoke(args=["create-admin", "--name", "B", "--email", "b@x.com",
                                "--phone", "1", "--password", "short"])
    assert short.exit_code != 0
    dup = runner.invoke(args=["create-admin", "--name", "B", "--email", "admin@x.com",
                              "--phone", "1", "--password", "longenough1"])
    assert dup.exit_code != 0
