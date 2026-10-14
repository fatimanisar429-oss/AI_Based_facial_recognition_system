"""Exit records: staff write down that a student has left. Nothing detects exits automatically."""
from flask import Blueprint, current_app, flash, g, redirect, render_template, request, url_for

from ..db import get_db
from ..security import role_required
from ..services import exits
from ..timeutil import day_bounds
from . import filters

bp = Blueprint("exits", __name__, url_prefix="/exits")


@bp.get("/")
@role_required("admin", "warden")
def index():
    cfg = current_app.config
    first, last = filters.date_range(request.args)
    q = request.args.get("q", "").strip()[:100]
    page = request.args.get("page", 1, type=int)
    db = get_db()
    start, end = day_bounds(first, last, cfg["TIMEZONE"], cfg["ATTENDANCE_DAY_START"])
    result = exits.search(db, start, end, q, page)
    students = db.execute("SELECT student_id, full_name, registration_no FROM student WHERE is_active = 1 "
                          "ORDER BY full_name COLLATE NOCASE").fetchall()
    options = [(s["student_id"], f"{s['full_name']} ({s['registration_no']})") for s in students]
    return render_template("exits/index.html", title="Exit records", result=result, student_options=options,
                           guests_inside=exits.guests_inside(db), args={"from": first, "to": last, "q": q})


@bp.post("/record-guest")
@role_required("admin", "warden")
def record_guest():
    guest_id = request.form.get("guest_id", type=int)
    outcome, _ = exits.record_guest_exit(get_db(), guest_id, g.user["role"], g.user["id"]) if guest_id else ("missing", None)
    messages = {
        "saved": ("Guest exit recorded.", "success"),
        "duplicate": ("An exit for this guest was already recorded a moment ago.", "info"),
        "not_inside": ("That guest is not recorded as inside, so there is no exit to record.", "warning"),
        "missing": ("Choose a guest from the list.", "warning"),
    }
    text, kind = messages[outcome]
    flash(text, kind)
    if outcome == "saved":
        current_app.logger.info("Guest exit recorded: guest_id=%s by %s_id=%s", guest_id, g.user["role"], g.user["id"])
    return redirect(url_for("exits.index"))


@bp.post("/record")
@role_required("admin", "warden")
def record():
    student_id = request.form.get("student_id", type=int)
    outcome, _ = exits.record_student_exit(get_db(), student_id, g.user["role"], g.user["id"]) if student_id else ("missing", None)
    messages = {
        "saved": ("Exit recorded.", "success"),
        "duplicate": ("An exit for this student was already recorded a moment ago.", "info"),
        "inactive": ("That student's account is deactivated.", "warning"),
        "missing": ("Choose a student from the list.", "warning"),
    }
    text, kind = messages[outcome]
    flash(text, kind)
    if outcome == "saved":
        current_app.logger.info("Exit recorded: student_id=%s by %s_id=%s", student_id, g.user["role"], g.user["id"])
    return redirect(url_for("exits.index"))
