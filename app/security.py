"""Security building blocks: who is logged in, role checks, CSRF, safe headers and error pages.

Everything here runs on the SERVER. Hiding a button in HTML is never enough, so every
protected route uses one of the decorators below.
"""
import hmac
import secrets
from functools import wraps

from flask import abort, current_app, flash, g, redirect, render_template, request, session, url_for

from .db import get_db

# role name -> (table, primary-key column). Only these three values are ever put in SQL.
ROLE_TABLES = {
    "admin": ("admin", "admin_id"),
    "warden": ("warden", "warden_id"),
    "student": ("student", "student_id"),
}

DASHBOARD_ENDPOINTS = {
    "admin": "dashboards.admin_dashboard",
    "warden": "dashboards.warden_dashboard",
    "student": "dashboards.student_dashboard",
}


def dashboard_url(role):
    return url_for(DASHBOARD_ENDPOINTS[role])


# ---------- who is logged in ----------

def load_current_user():
    """Runs before every request. Sets g.user from the signed session cookie.

    The user is re-read from the database each time, so a deactivated account
    is logged out on its very next request.
    """
    g.user = None
    role = session.get("role")
    user_id = session.get("user_id")
    if role not in ROLE_TABLES or user_id is None:
        return
    table, pk = ROLE_TABLES[role]
    row = get_db().execute(
        f"SELECT * FROM {table} WHERE {pk} = ? AND is_active = 1", (user_id,)
    ).fetchone()
    if row is None:
        session.clear()
        return
    user = {k: row[k] for k in row.keys() if k != "password_hash"}  # never keep the hash around
    user["role"] = role
    user["id"] = row[pk]
    g.user = user


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)
    return wrapped


def role_required(*roles):
    """Allow only the listed roles. Logged-out -> login page. Wrong role -> 403."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if g.user is None:
                flash("Please log in to continue.", "warning")
                return redirect(url_for("auth.login"))
            if g.user["role"] not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def local_only(view):
    """Allow the request only from the addresses listed in TERMINAL_ADDRESSES (the gate computer itself).
    The entry terminal needs no login, so it must not be reachable from other machines."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if request.remote_addr not in current_app.config["TERMINAL_ADDRESSES"]:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


# ---------- CSRF ----------

def csrf_token():
    """Per-session random token. Every form must include it as a hidden field."""
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(32)
    return session["_csrf"]


def check_csrf():
    """Reject any state-changing request that does not carry the session's token."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
        expected = session.get("_csrf", "")
        if not expected or not hmac.compare_digest(sent, expected):
            abort(400)


# ---------- headers and error pages ----------

def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    # The camera may be used by this site only (never by embedded content); microphone and location never.
    response.headers.setdefault("Permissions-Policy", "camera=(self), microphone=(), geolocation=()")
    if g.get("user"):
        response.headers["Cache-Control"] = "no-store"  # don't keep private pages in the browser cache
    return response


ERROR_MESSAGES = {
    400: ("Request rejected", "The security check failed. Go back, reload the page and try again."),
    403: ("Access denied", "You do not have permission to open this page."),
    404: ("Page not found", "The page you asked for does not exist."),
    405: ("Not allowed", "This action is not allowed here."),
    413: ("Upload too large", "The files you sent are too large."),
    500: ("Something went wrong", "An internal error occurred. Please try again later."),
}


def error_page(error):
    code = getattr(error, "code", 500)
    title, message = ERROR_MESSAGES.get(code, ERROR_MESSAGES[500])
    return render_template("error.html", code=code, title=title, message=message), code


def init_app(app):
    app.before_request(load_current_user)
    app.before_request(check_csrf)
    app.after_request(add_security_headers)
    # A Jinja *global* (not a context variable) so macros imported from other files can call csrf_token() too.
    app.jinja_env.globals["csrf_token"] = csrf_token
    app.context_processor(lambda: {"current_user": g.get("user")})
    for code in ERROR_MESSAGES:
        app.register_error_handler(code, error_page)
