"""Phase 12 security review, as tests.

POLICY below is written by hand from the SRS roles. It is the specification; the tests compare the real app against it, so
a new route without an entry here, or a route that is more open than intended, makes a test fail.
"""
import re
import subprocess
from pathlib import Path

import pytest

from conftest import get_token, login, token_when_logged_in

ROOT = Path(__file__).resolve().parent.parent
ADMIN, WARDEN, STUDENT = {"admin"}, {"warden"}, {"student"}
STAFF = ADMIN | WARDEN

# endpoint -> roles allowed to use it
POLICY = {
    "dashboards.admin_dashboard": ADMIN, "dashboards.warden_dashboard": WARDEN, "dashboards.student_dashboard": STUDENT,
    "students.index": STAFF, "students.detail": STAFF, "students.create": ADMIN, "students.edit": ADMIN,
    "students.activate": ADMIN, "students.deactivate": ADMIN,
    "wardens.index": ADMIN, "wardens.detail": ADMIN, "wardens.create": ADMIN, "wardens.edit": ADMIN,
    "wardens.activate": ADMIN, "wardens.deactivate": ADMIN,
    "enrollment.index": ADMIN, "enrollment.capture": ADMIN, "enrollment.check": ADMIN, "enrollment.save": ADMIN, "enrollment.remove": ADMIN,
    "attendance.index": STAFF, "attendance.mine": STUDENT,
    "logs.index": STAFF, "exits.index": STAFF, "exits.record": STAFF, "exits.record_guest": STAFF,
    "reports.index": STAFF,
    "settings.index": ADMIN, "contacts.index": ADMIN, "contacts.create": ADMIN, "contacts.edit": ADMIN, "contacts.delete": ADMIN,
    "emails.index": ADMIN, "emails.test": ADMIN, "emails.retry": ADMIN,
    "alerts.index": STAFF, "alerts.acknowledge": WARDEN, "alerts.mine": STUDENT, "alerts.emergency": STUDENT,
    "guests.mine": STUDENT, "guests.new": STUDENT, "guests.mine_detail": STUDENT,
    "guests.requests_list": STAFF, "guests.review": STAFF, "guests.decide": WARDEN,
    "guests.enroll": STAFF, "guests.enroll_check": STAFF, "guests.enroll_save": STAFF, "guests.enroll_remove": STAFF,
}
PUBLIC = {"home", "health", "auth.login", "auth.logout", "auth.dev_login"}      # logout/login/dev-login do their own checks
TERMINAL = {"terminal.index", "terminal.verify"}                                 # no login, but only from the gate computer
EMAILS = {"admin": "admin@x.com", "warden": "warden@x.com", "student": "s1@x.com"}


def routes(app):
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        url = re.sub(r"<int:\w+>", "999", rule.rule)
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            yield rule.endpoint, method, url


def protected(app):
    return [(e, m, u) for e, m, u in routes(app) if e not in PUBLIC and e not in TERMINAL]


def send(client, method, url, token=None):
    data = {"csrf_token": token} if token else {}
    return client.post(url, data=data) if method == "POST" else client.get(url)


def client_for(app, role):
    c = app.test_client()
    if role:
        assert login(c, EMAILS[role]).status_code == 302
    return c


def test_every_route_has_a_security_policy(app):
    known = set(POLICY) | PUBLIC | TERMINAL
    assert {e for e, _, _ in routes(app)} <= known, "a route has no entry in POLICY"
    assert set(POLICY) <= {e for e, _, _ in routes(app)}, "POLICY names a route that no longer exists"


def test_anonymous_visitors_are_sent_to_login(app, users):
    c = app.test_client()
    token = get_token(c)
    for endpoint, method, url in protected(app):
        resp = send(c, method, url, token)
        assert resp.status_code == 302 and "/login" in resp.headers["Location"], f"{method} {url} -> {resp.status_code}"


@pytest.mark.parametrize("role", ["admin", "warden", "student"])
def test_wrong_role_gets_403_and_right_role_does_not(app, users, role):
    c = client_for(app, role)
    token = token_when_logged_in(c)
    for endpoint, method, url in protected(app):
        resp = send(c, method, url, token)
        if role in POLICY[endpoint]:
            assert resp.status_code != 403 and resp.status_code < 500, f"{role} should reach {method} {url}, got {resp.status_code}"
        else:
            assert resp.status_code == 403, f"{role} must NOT reach {method} {url}, got {resp.status_code}"


@pytest.mark.parametrize("role", [None, "admin", "warden", "student"])
def test_every_post_without_a_csrf_token_is_rejected(app, users, role):
    c = client_for(app, role)
    for endpoint, method, url in routes(app):
        if method == "POST" and endpoint != "terminal.verify":
            assert c.post(url, data={}).status_code == 400, f"{role}: POST {url} accepted without a CSRF token"
    assert c.post("/terminal/verify", data={}).status_code == 400
    assert c.post("/login", data={"email": "admin@x.com", "password": "x", "csrf_token": "forged"}).status_code == 400


def test_a_token_from_another_session_is_rejected(app, users):
    other = app.test_client()
    stolen = get_token(other)
    c = client_for(app, "admin")
    assert c.post("/contacts/1/delete", data={"csrf_token": stolen}).status_code == 400
    assert c.post("/logout", data={"csrf_token": stolen}).status_code == 400


def test_logout_is_post_only(app, users):
    c = client_for(app, "admin")
    assert c.get("/logout").status_code == 405
    assert c.get("/admin/").status_code == 200


@pytest.mark.parametrize("path", ["/terminal/", "/terminal/verify"])
def test_the_entry_terminal_only_answers_the_gate_computer(app, users, path):
    elsewhere = {"REMOTE_ADDR": "192.168.1.50"}
    c = app.test_client()
    token = get_token(c)
    assert c.get(path, environ_overrides=elsewhere).status_code in (403, 405)
    assert c.post(path, data={"csrf_token": token}, environ_overrides=elsewhere).status_code in (403, 405)
    # ...even for a logged-in administrator on another machine
    admin = client_for(app, "admin")
    assert admin.get("/terminal/", environ_overrides=elsewhere).status_code == 403
    assert admin.get("/terminal/", environ_overrides={"REMOTE_ADDR": "127.0.0.1"}).status_code == 200


def test_dev_quick_login_is_off_by_default(app, users):
    c = app.test_client()
    token = get_token(c)
    assert "Quick login" not in c.get("/login").get_data(as_text=True)
    assert c.post("/dev-login/admin", data={"csrf_token": token}).status_code in (403, 404)
    assert c.get("/admin/").status_code == 302


def test_passwords_are_hashed_and_never_in_pages_or_session(app, users):
    import sqlite3
    con = sqlite3.connect(app.config["DATABASE_PATH"])
    hashes = [r[0] for r in con.execute("SELECT password_hash FROM admin UNION SELECT password_hash FROM warden UNION SELECT password_hash FROM student")]
    con.close()
    assert hashes and all(h.startswith(("scrypt:", "pbkdf2:")) for h in hashes)
    c = client_for(app, "admin")
    for url in ("/admin/", "/students/", "/students/1", "/students/1/edit", "/wardens/", "/settings/", "/emails/"):
        page = c.get(url).get_data(as_text=True)
        assert not any(h in page for h in hashes) and "password_hash" not in page, url
    with c.session_transaction() as s:
        assert set(s) <= {"user_id", "role", "_csrf", "_flashes", "_permanent"}


def test_error_pages_do_not_leak_details(app, users):
    c = client_for(app, "admin")
    for url, code in (("/nope", 404), ("/students/99999", 404), ("/logout", 405)):
        resp = c.get(url)
        text = resp.get_data(as_text=True)
        assert resp.status_code == code and "Traceback" not in text and str(ROOT) not in text and "sqlite" not in text.lower()


def test_every_page_sends_the_security_headers(app, users):
    resp = client_for(app, "admin").get("/admin/")
    assert resp.headers["X-Content-Type-Options"] == "nosniff" and resp.headers["X-Frame-Options"] == "DENY"
    assert "camera=(self)" in resp.headers["Permissions-Policy"] and "microphone=()" in resp.headers["Permissions-Policy"]
    assert resp.headers["Cache-Control"] == "no-store"


def test_session_cookie_is_http_only_and_same_site(app, users):
    cookie = app.test_client().get("/login").headers.get("Set-Cookie", "")
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie


def test_sql_is_never_built_from_user_input():
    """Every f-string SQL statement may only splice in names/placeholders that the code itself defines."""
    allowed_names = {"table", "pk", "base", "where", "marks", "sets", "cols", "column", "ddl", "ORDERS"}
    offenders = []
    for path in (ROOT / "app").rglob("*.py"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            literal = re.search(r"execute(?:script)?\(\s*f(\"[^\"]*\"|'[^']*')", line)
            if literal:
                for expr in re.findall(r"\{([^}]*)\}", literal.group(1)):
                    names = set(re.findall(r"[A-Za-z_]\w*", expr)) - {"join", "d", "c", "for"}
                    if not names <= allowed_names and not re.fullmatch(r"\s*'[^']*'\.join\(.*\)\s*", expr):
                        offenders.append(f"{path.name}:{number}: {{{expr}}}")
    assert not offenders, offenders


def test_secrets_and_data_files_are_git_ignored():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for pattern in (".env", "instance/", "*.db", ".venv/"):
        assert pattern in ignored
    git = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
    if git.returncode == 0:
        tracked = git.stdout.splitlines()
        assert not [f for f in tracked if f == ".env" or f.endswith((".db", ".sqlite3")) or f.startswith("instance/")]


def test_env_example_holds_only_placeholders():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    values = dict(line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith("#"))
    for secret in ("MAIL_PASSWORD", "MAIL_USERNAME", "BIOMETRIC_KEY", "MAIL_SERVER", "MAIL_DEFAULT_SENDER"):
        assert values[secret] == "", f"{secret} must be empty in .env.example"
    assert values["SECRET_KEY"] == "change-me" and values["DEV_QUICK_LOGIN"] == "0" and values["FLASK_DEBUG"] == "0"
    assert not re.search(r"[A-Za-z0-9_\-]{40,}={0,2}\s*$", text, re.M), "something in .env.example looks like a real key"
