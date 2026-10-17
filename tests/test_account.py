"""My account page: every role can change their own email and password, and nobody else's."""
import pytest
from werkzeug.security import check_password_hash

from app.db import get_db
from conftest import PASSWORD, login, token_when_logged_in

ROLES = {"admin": ("admin", "admin@x.com"), "warden": ("warden", "warden@x.com"), "student": ("student", "s1@x.com")}


def stored(app, table, email):
    with app.app_context():
        return get_db().execute(f"SELECT * FROM {table} WHERE email = ?", (email,)).fetchone()


def logged_in(app, role):
    c = app.test_client()
    assert login(c, ROLES[role][1]).status_code == 302
    return c, token_when_logged_in(c)


@pytest.mark.parametrize("role", ROLES)
def test_every_role_sees_the_page_and_a_menu_link(app, users, role):
    c, _ = logged_in(app, role)
    page = c.get("/account/")
    assert page.status_code == 200 and b"Change email" in page.data and b"Change password" in page.data
    assert b'href="/account/"' in page.data          # the menu link


@pytest.mark.parametrize("role", ROLES)
def test_change_email_then_login_with_the_new_one(app, users, role):
    table, old = ROLES[role]
    c, token = logged_in(app, role)
    r = c.post("/account/email", data={"csrf_token": token, "email": "New.Mail@X.com", "email_current_password": PASSWORD})
    assert r.status_code == 302
    assert stored(app, table, "new.mail@x.com") is not None and stored(app, table, old) is None
    fresh = app.test_client()
    assert login(fresh, "new.mail@x.com").status_code == 302
    assert login(app.test_client(), old).status_code == 401


def test_change_email_needs_the_right_current_password(app, users):
    c, token = logged_in(app, "student")
    r = c.post("/account/email", data={"csrf_token": token, "email": "n@x.com", "email_current_password": "wrong-password"})
    assert r.status_code == 400 and b"current password is not correct" in r.data
    assert stored(app, "student", "s1@x.com") is not None


@pytest.mark.parametrize("bad, expected", [("", b"required"), ("not-an-email", b"valid"), ("warden@x.com", b"already used"),
                                           ("S2@x.com", b"already used")])
def test_change_email_rejects_bad_or_taken_emails(app, users, bad, expected):
    c, token = logged_in(app, "student")
    r = c.post("/account/email", data={"csrf_token": token, "email": bad, "email_current_password": PASSWORD})
    assert r.status_code == 400 and expected in r.data
    assert stored(app, "student", "s1@x.com") is not None


def test_keeping_my_own_email_is_allowed(app, users):
    c, token = logged_in(app, "warden")
    r = c.post("/account/email", data={"csrf_token": token, "email": "warden@x.com", "email_current_password": PASSWORD})
    assert r.status_code == 302


@pytest.mark.parametrize("role", ROLES)
def test_change_password_then_login_with_the_new_one(app, users, role):
    table, email = ROLES[role]
    c, token = logged_in(app, role)
    r = c.post("/account/password", data={"csrf_token": token, "current_password": PASSWORD, "new_password": "brand-new-pass-9",
                                          "confirm_password": "brand-new-pass-9"})
    assert r.status_code == 302
    row = stored(app, table, email)
    assert check_password_hash(row["password_hash"], "brand-new-pass-9") and "brand-new-pass-9" not in row["password_hash"]
    assert c.get("/account/").status_code == 200                      # still logged in
    assert login(app.test_client(), email).status_code == 401           # old password no longer works
    assert login(app.test_client(), email, "brand-new-pass-9").status_code == 302


@pytest.mark.parametrize("current, new, confirm, expected", [
    ("wrong-password", "brand-new-pass-9", "brand-new-pass-9", b"current password is not correct"),
    (PASSWORD, "short", "short", b"8-128 characters"),
    (PASSWORD, "brand-new-pass-9", "different-pass-99", b"do not match"),
    (PASSWORD, PASSWORD, PASSWORD, b"must be different"),
])
def test_change_password_rejects_bad_input(app, users, current, new, confirm, expected):
    c, token = logged_in(app, "student")
    r = c.post("/account/password", data={"csrf_token": token, "current_password": current, "new_password": new,
                                          "confirm_password": confirm})
    assert r.status_code == 400 and expected in r.data
    assert check_password_hash(stored(app, "student", "s1@x.com")["password_hash"], PASSWORD)


def test_a_form_cannot_name_another_account(app, users):
    """The account being edited comes from the session; extra fields such as student_id/role are ignored."""
    c, token = logged_in(app, "student")
    c.post("/account/email", data={"csrf_token": token, "email": "mine@x.com", "email_current_password": PASSWORD,
                                   "student_id": "2", "role": "admin", "id": "1"})
    assert stored(app, "student", "mine@x.com")["registration_no"] == "REG-001"
    assert stored(app, "student", "s2@x.com") is not None and stored(app, "admin", "admin@x.com") is not None


def test_repeated_wrong_current_passwords_lock_the_account(app, users):
    c, token = logged_in(app, "student")
    for _ in range(5):
        c.post("/account/password", data={"csrf_token": token, "current_password": "nope", "new_password": "brand-new-pass-9",
                                          "confirm_password": "brand-new-pass-9"})
    r = c.post("/account/password", data={"csrf_token": token, "current_password": PASSWORD, "new_password": "brand-new-pass-9",
                                          "confirm_password": "brand-new-pass-9"})
    assert r.status_code == 400 and b"Too many failed attempts" in r.data
    assert check_password_hash(stored(app, "student", "s1@x.com")["password_hash"], PASSWORD)


def test_logged_out_visitors_are_sent_to_login(app, users):
    c = app.test_client()
    assert c.get("/account/").status_code == 302
