"""Sidebar menu for each role.

Each entry is (label, endpoint, icon). An endpoint of None means the page is built in a later
phase: it is shown greyed out with a "Soon" tag. When a phase adds the page, put its endpoint
here and the link becomes live. Set SHOW_UPCOMING = False to hide unfinished entries completely.
"""
from flask import g, request, url_for

from .db import get_db
from .services import alerts as alert_service

SHOW_UPCOMING = True

NAV = {
    "admin": [
        ("Dashboard", "dashboards.admin_dashboard", "grid"),
        ("Students", "students.index", "users"),
        ("Wardens", "wardens.index", "shield"),
        ("Enrollment", "enrollment.index", "camera"),
        ("Entry terminal", "terminal.index", "log-in"),
        ("Attendance", "attendance.index", "calendar"),
        ("Entry logs", "logs.index", "list"),
        ("Exit records", "exits.index", "log-out"),
        ("Guest visits", "guests.requests_list", "user-plus"),
        ("Alerts", "alerts.index", "bell"),
        ("Emergency contacts", "contacts.index", "phone"),
        ("Email", "emails.index", "mail"),
        ("Reports", "reports.index", "file-text"),
        ("Settings", "settings.index", "sliders"),
    ],
    "warden": [
        ("Dashboard", "dashboards.warden_dashboard", "grid"),
        ("Students", "students.index", "users"),
        ("Entry terminal", "terminal.index", "log-in"),
        ("Attendance", "attendance.index", "calendar"),
        ("Entry logs", "logs.index", "list"),
        ("Exit records", "exits.index", "log-out"),
        ("Guest requests", "guests.requests_list", "user-plus"),
        ("Alerts", "alerts.index", "bell"),
        ("Reports", "reports.index", "file-text"),
    ],
    "student": [
        ("Dashboard", "dashboards.student_dashboard", "grid"),
        ("My attendance", "attendance.mine", "calendar"),
        ("Guest requests", "guests.mine", "user-plus"),
        ("Emergency alert", "alerts.mine", "alert-triangle"),
    ],
}


def nav_items():
    """Menu entries for the logged-in user, ready for the template."""
    if not g.get("user"):
        return []
    items = []
    new_alerts = alert_service.new_count(get_db()) if g.user["role"] in ("admin", "warden") else 0
    for label, endpoint, icon in NAV[g.user["role"]]:
        if endpoint is None and not SHOW_UPCOMING:
            continue
        items.append({
            "label": label,
            "icon": icon,
            "url": url_for(endpoint) if endpoint else None,
            "badge": new_alerts if endpoint == "alerts.index" else 0,
            # A section stays highlighted on its sub-pages (students.index, students.edit, ...).
            "active": endpoint is not None and request.blueprint == endpoint.split(".")[0],
        })
    return items


def initials(name):
    parts = (name or "").split()
    return "".join(p[0] for p in parts[:2]).upper() or "?"


def init_app(app):
    app.context_processor(lambda: {"nav_items": nav_items()})
    app.jinja_env.filters["initials"] = initials
