"""Local-testing quick login: off by default, local only, CSRF-protected, and the demo-users command."""
import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from app.db import get_db
from conftest import PASSWORD, get_token, login

LOCAL = {"REMOTE_ADDR": "127.0.0.1"}
REMOTE = {"REMOTE_ADDR": "192.168.1.50"}


@pytest.fixture
def dev_app(tmp_path):
    app = create_app({"TESTING": True, "SECRET_KEY": "k", "DATABASE_PATH": str(tmp_path / "dev.db"), "DEV_QUICK_LOGIN": True})
    runner = app.test_cli_runner()
    assert runner.invoke(args=["create-demo-users"]).exit_code == 0
    return app


def press(client, target, remote=LOCAL, token=None):
    token = token or get_token(client)
    return client.post(f"/dev-login/{target}", data={"csrf_token": token}, environ_overrides=remote)


# ---------- off by default ----------

def test_the_feature_is_off_unless_switched_on(app, users):
    client = app.test_client()
    html = client.get("/login").get_data(as_text=True)
    assert "Local testing only" not in html and "dev-login" not in html
    assert press(client, "admin").status_code == 404


def test_the_example_settings_file_ships_with_it_switched_off():
    assert "DEV_QUICK_LOGIN=0" in open(".env.example", encoding="utf-8").read()


# ---------- when on ----------

def test_login_page_shows_the_panel_on_this_computer(dev_app):
    html = dev_app.test_client().get("/login", environ_overrides=LOCAL).get_data(as_text=True)
    assert 'id="dev-panel"' in html and "Local testing only" in html
    for target in ("admin", "warden", "student", "enroll"):
        assert f'/dev-login/{target}"' in html
    assert "Register a face" in html and 'href="/terminal/"' in html


@pytest.mark.parametrize("target,landing,role_path", [
    ("admin", "/admin/", "/admin/"), ("warden", "/warden/", "/warden/"), ("student", "/student/", "/student/")])
def test_one_click_logs_in_as_the_demo_role(dev_app, target, landing, role_path):
    client = dev_app.test_client()
    resp = press(client, target)
    assert resp.status_code == 302 and resp.headers["Location"].endswith(landing)
    assert client.get(role_path).status_code == 200
    other = {"admin": "/warden/", "warden": "/admin/", "student": "/admin/"}[target]
    assert client.get(other).status_code == 403                 # still only that role's pages


def test_register_a_face_button_goes_to_the_demo_students_enrollment_page(dev_app):
    client = dev_app.test_client()
    resp = press(client, "enroll")
    with dev_app.app_context():
        sid = get_db().execute("SELECT student_id FROM student WHERE email='student@hostel.local'").fetchone()[0]
    assert resp.status_code == 302 and resp.headers["Location"].endswith(f"/enrollment/student/{sid}")
    page = client.get(resp.headers["Location"])
    assert page.status_code == 200 and "Demo Student" in page.get_data(as_text=True)
    assert client.get("/admin/").status_code == 200              # it signed in as the admin


def test_quick_login_session_has_the_normal_idle_timeout(dev_app):
    client = dev_app.test_client()
    resp = press(client, "admin")
    assert "expires" in resp.headers["Set-Cookie"].lower() or "max-age" in resp.headers["Set-Cookie"].lower()


# ---------- guard rails ----------

def test_other_computers_get_nothing(dev_app):
    client = dev_app.test_client()
    html = client.get("/login", environ_overrides=REMOTE).get_data(as_text=True)
    assert "Local testing only" not in html
    token = get_token(client)
    assert press(client, "admin", remote=REMOTE, token=token).status_code == 403
    assert client.get("/admin/", environ_overrides=REMOTE).status_code == 302          # not logged in


def test_csrf_token_is_required(dev_app):
    client = dev_app.test_client()
    client.get("/login")
    assert client.post("/dev-login/admin", environ_overrides=LOCAL).status_code == 400
    assert client.post("/dev-login/admin", data={"csrf_token": "forged"}, environ_overrides=LOCAL).status_code == 400
    assert client.get("/admin/").status_code == 302


def test_get_is_not_allowed(dev_app):
    assert dev_app.test_client().get("/dev-login/admin", environ_overrides=LOCAL).status_code == 405


@pytest.mark.parametrize("target", ["root", "superuser", "ADMIN", "1", "student/../admin"])
def test_unknown_targets_are_404(dev_app, target):
    assert press(dev_app.test_client(), target).status_code == 404


def test_missing_demo_account_is_explained(tmp_path):
    app = create_app({"TESTING": True, "SECRET_KEY": "k", "DATABASE_PATH": str(tmp_path / "e.db"), "DEV_QUICK_LOGIN": True})
    client = app.test_client()
    resp = press(client, "warden")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/login")
    assert b"create-demo-users" in client.get("/login", environ_overrides=LOCAL).data


def test_deactivated_demo_account_cannot_be_used(dev_app):
    with dev_app.app_context():
        get_db().execute("UPDATE warden SET is_active = 0 WHERE email = 'warden@hostel.local'")
        get_db().commit()
    client = dev_app.test_client()
    resp = press(client, "warden")
    assert resp.headers["Location"].endswith("/login") and client.get("/warden/").status_code == 302


def test_a_real_account_with_a_demo_email_but_the_wrong_role_is_refused(dev_app):
    """If the 'warden' demo email were taken by a student, the button must not log in as that student."""
    with dev_app.app_context():
        db = get_db()
        db.execute("UPDATE warden SET email = 'other@hostel.local'")
        db.execute("UPDATE student SET email = 'warden@hostel.local'")
        db.commit()
    client = dev_app.test_client()
    resp = press(client, "warden")
    assert resp.headers["Location"].endswith("/login")
    assert client.get("/student/").status_code == 302


def test_normal_password_login_still_works_when_enabled(dev_app):
    with dev_app.app_context():
        get_db().execute("UPDATE admin SET password_hash = ? WHERE email = 'admin@hostel.local'", (generate_password_hash(PASSWORD),))
        get_db().commit()
    assert login(dev_app.test_client(), "admin@hostel.local").status_code == 302


# ---------- the demo-users command ----------

def test_command_creates_linked_demo_accounts_with_unknown_passwords(dev_app):
    with dev_app.app_context():
        db = get_db()
        assert db.execute("SELECT COUNT(*) FROM admin").fetchone()[0] == 1
        warden = db.execute("SELECT warden_id, password_hash FROM warden").fetchone()
        student = db.execute("SELECT * FROM student").fetchone()
    assert student["warden_id"] == warden["warden_id"] and student["registration_no"] == "DEMO-001"
    client = dev_app.test_client()
    for email in ("admin@hostel.local", "warden@hostel.local", "student@hostel.local"):
        for guess in ("Demo@12345", "password", "Hostel@Admin123", ""):
            assert login(client, email, guess).status_code in (400, 401)


def test_command_is_safe_to_repeat(dev_app):
    result = dev_app.test_cli_runner().invoke(args=["create-demo-users"])
    assert result.exit_code == 0 and "nothing" in result.output
    with dev_app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM student").fetchone()[0] == 1


def test_command_keeps_an_existing_admin_with_that_email(tmp_path):
    app = create_app({"TESTING": True, "SECRET_KEY": "k", "DATABASE_PATH": str(tmp_path / "k.db"), "DEV_QUICK_LOGIN": True})
    with app.app_context():
        get_db().execute("INSERT INTO admin (full_name,email,phone,password_hash) VALUES ('Real Admin','admin@hostel.local','1',?)",
                         (generate_password_hash("my-own-password"),))
        get_db().commit()
    app.test_cli_runner().invoke(args=["create-demo-users"])
    with app.app_context():
        rows = get_db().execute("SELECT full_name FROM admin").fetchall()
    assert [r[0] for r in rows] == ["Real Admin"]
    assert login(app.test_client(), "admin@hostel.local", "my-own-password").status_code == 302
