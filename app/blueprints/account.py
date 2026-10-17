"""My account: every logged-in person (admin, warden, student) can change their own email and password.

The person is always taken from the session (g.user), never from the form, so nobody can edit someone else's account here.
Both changes ask for the CURRENT password, so a stolen or unattended session cannot take over the account.
"""
from flask import Blueprint, current_app, flash, g, redirect, render_template, request, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from ..db import get_db
from ..security import ROLE_TABLES, role_required
from ..services.people import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH, _check_email
from .auth import is_locked_out, record_failed_login, start_session

bp = Blueprint("account", __name__, url_prefix="/account")

ALL_ROLES = ("admin", "warden", "student")


def _own_row(db):
    table, pk = ROLE_TABLES[g.user["role"]]   # table and column names come from a fixed list, never from the browser
    return table, pk, db.execute(f"SELECT * FROM {table} WHERE {pk} = ?", (g.user["id"],)).fetchone()


def _current_password_error(row, field):
    """None if the typed current password is right. Wrong guesses count towards the same lockout as the login page."""
    email = row["email"]
    if is_locked_out(email):
        return "Too many failed attempts. Try again later."
    if not check_password_hash(row["password_hash"], request.form.get(field, "")):
        record_failed_login(email)
        return "Your current password is not correct."
    return None


def _page(email_errors=None, password_errors=None, email_value=None, status=200):
    return render_template("account/index.html", title="My account", email_errors=email_errors or {},
                           password_errors=password_errors or {},
                           email_value=email_value if email_value is not None else g.user["email"]), status


@bp.get("/")
@role_required(*ALL_ROLES)
def index():
    return _page()


@bp.post("/email")
@role_required(*ALL_ROLES)
def update_email():
    db = get_db()
    table, pk, row = _own_row(db)
    data, errors = {}, {}
    _check_email(db, request.form, data, errors, exclude=(g.user["role"], g.user["id"]))
    if "email" not in errors:
        problem = _current_password_error(row, "email_current_password")
        if problem:
            errors["email_current_password"] = problem
    if errors:
        return _page(email_errors=errors, email_value=data.get("email"), status=400)
    db.execute(f"UPDATE {table} SET email = ? WHERE {pk} = ?", (data["email"], g.user["id"]))
    db.commit()
    current_app.logger.info("Email changed for %s id=%s", g.user["role"], g.user["id"])
    flash("Your email was updated. Use the new email the next time you log in.", "success")
    return redirect(url_for("account.index"))


@bp.post("/password")
@role_required(*ALL_ROLES)
def update_password():
    db = get_db()
    table, pk, row = _own_row(db)
    new = request.form.get("new_password", "")
    errors = {}
    if not MIN_PASSWORD_LENGTH <= len(new) <= MAX_PASSWORD_LENGTH:
        errors["new_password"] = f"New password must be {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} characters."
    elif new != request.form.get("confirm_password", ""):
        errors["confirm_password"] = "The two new passwords do not match."
    elif check_password_hash(row["password_hash"], new):
        errors["new_password"] = "The new password must be different from the current one."
    if not errors:
        problem = _current_password_error(row, "current_password")
        if problem:
            errors["current_password"] = problem
    if errors:
        return _page(password_errors=errors, status=400)
    db.execute(f"UPDATE {table} SET password_hash = ? WHERE {pk} = ?", (generate_password_hash(new), g.user["id"]))
    db.commit()
    start_session(g.user["role"], _own_row(db)[2])    # fresh session after a password change
    current_app.logger.info("Password changed for %s id=%s", g.user["role"], g.user["id"])
    flash("Your password was changed.", "success")
    return redirect(url_for("account.index"))
