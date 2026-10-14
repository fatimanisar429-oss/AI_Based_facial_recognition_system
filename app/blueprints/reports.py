"""Reports page (SRS FR-12, UC-08): daily, weekly and monthly attendance and security reports. Admin and warden."""
from flask import Blueprint, current_app, render_template, request

from ..db import get_db
from ..security import role_required
from ..services import attendance as attendance_service
from ..services import reports
from ..services.charts import column_chart
from ..timeutil import format_local, parse_date, utc_now_text
from . import filters

bp = Blueprint("reports", __name__, url_prefix="/reports")

TABS = ("attendance", "security", "parents")


@bp.get("/")
@role_required("admin", "warden")
def index():
    cfg, db = current_app.config, get_db()
    today = filters.today()
    kind = request.args.get("type", "weekly")
    kind = kind if kind in reports.REPORT_TYPES else "weekly"
    tab = request.args.get("tab", "attendance")
    tab = tab if tab in TABS else "attendance"
    day = parse_date(request.args.get("date"), today)
    week = parse_date(request.args.get("week"), today)
    month = attendance_service.valid_month(request.args.get("month"), today[:7])
    first, last, title = reports.period_for(kind, day, week, month, today)
    q = request.args.get("q", "").strip()[:100]
    order = request.args.get("order", "name")
    order = order if order in reports.ORDERS else "name"
    status = request.args.get("status", "")
    page = request.args.get("page", 1, type=int)

    context = {"title": "Reports", "kind": kind, "tab": tab, "report_title": title, "first": first, "last": last,
               "values": {"type": kind, "date": day, "week": week, "month": month, "q": q, "order": order},
               "generated": format_local(utc_now_text(), cfg["TIMEZONE"], "%d %b %Y, %I:%M %p"), "today": today}
    if tab == "attendance":
        context["att"] = reports.attendance_report(db, cfg, first, last, today, kind, q, order)
    elif tab == "security":
        sec = reports.security_report(db, cfg, first, last)
        sec["chart"] = column_chart(sec["chart_days"], ["Granted", "Denied"])
        context["sec"] = sec
    else:
        context["parents"] = reports.parent_reports(db, first, last, status, q, page)
        context["status"] = status
    return render_template("reports/index.html", **context)