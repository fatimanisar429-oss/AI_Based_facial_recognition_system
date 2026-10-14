"""Login and logout (SRS FR-01, UC-01)."""
from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from ..db import get_db
from ..security import ROLE_TABLES, dashboard_url
from ..timeutil import minutes_ago_text, utc_now_text

bp = Blueprint("auth", __name__)

MAX_FAILED_LOGINS = 5   # failed attempts allowed per email ...
LOCKOUT_MINUTES = 15    # ... within this many minutes

# Checked when the email is unknown, so "no such user" takes as long as "wrong password".
_DUMMY_HASH = generate_password_hash("not-a-real-password")


def find_account(email):
    """Look the email up in admin, warden, student. Returns (role, row) or None."""
    db = get_db()
    for role, (table, _pk) in ROLE_TABLES.items():
        row = db.execute(f"SELECT * FROM {table} WHERE email = ? COLLATE NOCASE", (email,)).fetchone()
        if row is not None:
            return role, row
    return None


def is_locked_out(email):
    since = minutes_ago_text(LOCKOUT_MINUTES)
    count = get_db().execute(
        "SELECT COUNT(*) FROM login_attempt WHERE email = ? AND attempted_at >= ?", (email, since)
    ).fetchone()[0]
    return count >= MAX_FAILED_LOGINS


def record_failed_login(email):
    db = get_db()
    db.execute("DELETE FROM login_attempt WHERE attempted_at < ?", (minutes_ago_text(LOCKOUT_MINUTES),))
    db.execute("INSERT INTO login_attempt (email, attempted_at) VALUES (?, ?)", (email[:254], utc_now_text()))
    db.commit()


def start_session(role, row):
    """Log the person in: replace any old session and remember who they are."""
    session.clear()  # discard any old session data (prevents session fixation)
    session["role"] = role
    session["user_id"] = row[ROLE_TABLES[role][1]]
    session.permanent = True  # enables the 15-minute idle timeout


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(dashboard_url(g.user["role"]))

    if request.method == "GET":
        return render_template("login.html")

    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")

    if not email or not password:
        return render_template("login.html", error="Enter your email and password.", email=email), 400

    if is_locked_out(email):
        return render_template(
            "login.html", email=email,
            error=f"Too many failed attempts. Try again in {LOCKOUT_MINUTES} minutes.",
        ), 429

    account = find_account(email)
    active = account is not None and account[1]["is_active"] == 1
    stored_hash = account[1]["password_hash"] if active else _DUMMY_HASH
    password_ok = check_password_hash(stored_hash, password)

    if not (active and password_ok):
        record_failed_login(email)
        # Same message for unknown email, wrong password and deactivated account.
        return render_template("login.html", error="Incorrect email or password.", email=email), 401

    role, row = account
    start_session(role, row)
    db = get_db()
    db.execute("DELETE FROM login_attempt WHERE email = ?", (email,))
    db.commit()
    return redirect(dashboard_url(role))


@bp.post("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("auth.login"))


# ---------- LOCAL TESTING ONLY: one-click login ----------
# Switched on with DEV_QUICK_LOGIN=1 in .env. Never enable it on a shared or deployed machine.

def dev_quick_login_enabled():
    """On only when switched on AND the request comes from this computer (the same rule as the entry terminal)."""
    return bool(current_app.config["DEV_QUICK_LOGIN"]) and request.remote_addr in current_app.config["TERMINAL_ADDRESSES"]


@bp.app_context_processor
def _dev_login_flag():
    return {"dev_quick": dev_quick_login_enabled()}


DEV_TARGETS = ("admin", "warden", "student", "enroll")      # "enroll" = admin, then straight to the face-enrollment page


@bp.post("/dev-login/<target>")
def dev_login(target):
    if not current_app.config["DEV_QUICK_LOGIN"] or target not in DEV_TARGETS:
        abort(404)
    if not dev_quick_login_enabled():
        abort(403)

    role = "admin" if target == "enroll" else target
    email = current_app.config["DEV_ACCOUNTS"][role]
    account = find_account(email)
    if account is None or account[0] != role or account[1]["is_active"] != 1:
        flash(f"The demo {role} account is missing or deactivated. Run: flask --app run create-demo-users", "warning")
        return redirect(url_for("auth.login"))
    start_session(role, account[1])
    current_app.logger.warning("DEV quick login used for the demo %s account", role)

    if target == "enroll":
        student = find_account(current_app.config["DEV_ACCOUNTS"]["student"])
        if student is None:
            flash("The demo student is missing. Run: flask --app run create-demo-users", "warning")
            return redirect(dashboard_url("admin"))
        return redirect(url_for("enrollment.capture", student_id=student[1]["student_id"]))
    return redirect(dashboard_url(role))
