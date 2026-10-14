"""Role dashboards (SRS UC-05, UC-07). Each page shows data for its own role only."""
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Blueprint, current_app, g, render_template

from ..db import get_db
from ..security import role_required
from ..services import appsettings, dashboard_data, guests
from ..timeutil import hostel_today, utc_now_text

bp = Blueprint("dashboards", __name__)


def _tz():
    return current_app.config["TIMEZONE"]


def _day_start():
    return current_app.config["ATTENDANCE_DAY_START"]


def _today():
    """Today's attendance day (starts at ATTENDANCE_DAY_START, so 00:30 still belongs to yesterday)."""
    return hostel_today(_tz(), current_app.config["ATTENDANCE_DAY_START"])


def _greeting():
    """('Good evening', 'Sunday, 11 October 2026') in the hostel's timezone."""
    now = datetime.now(ZoneInfo(_tz()))
    part = "morning" if now.hour < 12 else "afternoon" if now.hour < 17 else "evening"
    return f"Good {part}", now.strftime("%A, %d %B %Y")


@bp.get("/admin/")
@role_required("admin")
def admin_dashboard():
    data = dashboard_data.admin_dashboard(get_db(), _today(), _tz(), _day_start())
    return render_template("dashboards/admin.html", title="Admin Dashboard", d=data, hello=_greeting())


@bp.get("/warden/")
@role_required("warden")
def warden_dashboard():
    data = dashboard_data.warden_dashboard(get_db(), _today(), _tz(), _day_start())
    now = utc_now_text()
    return render_template("dashboards/warden.html", title="Warden Dashboard", d=data, hello=_greeting(),
                           pending_guests=guests.pending_list(get_db(), now), pending_count=guests.pending_count(get_db(), now),
                           now=now)


@bp.get("/student/")
@role_required("student")
def student_dashboard():
    # The student id comes from the logged-in session, never from the URL or a form.
    data = dashboard_data.student_dashboard(get_db(), g.user["id"], _today())
    now = utc_now_text()
    return render_template("dashboards/student.html", title="My Dashboard", d=data, hello=_greeting(),
                           guest_rows=guests.for_student(get_db(), g.user["id"], 3), now=now, status_of=guests.display_status,
                           curfew=appsettings.curfew_time(get_db(), current_app.config))
