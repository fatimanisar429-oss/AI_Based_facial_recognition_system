"""Attendance pages (SRS FR-09, FR-10, UC-05, UC-07).

Staff (admin, warden): all students, with filters.   Student: only their own records (/attendance/my)."""
from flask import Blueprint, current_app, g, render_template, request

from ..db import get_db
from ..security import role_required
from ..services import attendance
from . import filters

bp = Blueprint("attendance", __name__, url_prefix="/attendance")


@bp.get("/")
@role_required("admin", "warden")
def index():
    first, last = filters.date_range(request.args)
    status = request.args.get("status", "")
    status = status if status in attendance.STATUSES else ""
    q = request.args.get("q", "").strip()[:100]
    page = request.args.get("page", 1, type=int)
    result = attendance.search(get_db(), first, last, status, q, page)
    args = {"from": first, "to": last, "status": status, "q": q}
    return render_template("attendance/index.html", title="Attendance", result=result, args=args,
                           single_day=first == last)


@bp.get("/my")
@role_required("student")
def mine():
    # The student id always comes from the login session. Nothing in the URL can select another student.
    db = get_db()
    this_month = filters.today()[:7]
    month = attendance.valid_month(request.args.get("month"), this_month)
    data = attendance.for_student(db, g.user["id"], month)
    return render_template("attendance/mine.html", title="My attendance", month=month, data=data,
                           history=attendance.student_entry_history(db, g.user["id"]),
                           curfew=current_app.config["CURFEW_TIME"])
