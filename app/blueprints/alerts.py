"""Alerts (SRS FR-11, FR-14, FR-16).

Student : press the Emergency button (with a confirmation) and see their own alerts.
Warden  : see all alerts and ACKNOWLEDGE them. Admin: can see alerts (read only).
"""
from flask import Blueprint, current_app, flash, g, redirect, render_template, request, url_for

from ..db import get_db
from ..security import role_required
from ..services import alerts, emailer
from ..timeutil import day_bounds, utc_now_text
from . import filters

bp = Blueprint("alerts", __name__, url_prefix="/alerts")


@bp.get("/")
@role_required("admin", "warden")
def index():
    cfg = current_app.config
    alert_type = request.args.get("type", "")
    status = request.args.get("status", "")
    page = request.args.get("page", 1, type=int)
    args = {"type": alert_type if alert_type in alerts.TYPES else "", "status": status if status in alerts.STATUSES else ""}
    start = end = None
    if request.args.get("from") or request.args.get("to"):
        first, last = filters.date_range(request.args)
        start, end = day_bounds(first, last, cfg["TIMEZONE"], cfg["ATTENDANCE_DAY_START"])
        args.update({"from": first, "to": last})
    result = alerts.search(get_db(), args["type"], args["status"], start, end, page)
    return render_template("alerts/index.html", title="Alerts", result=result, args=args, types=alerts.TYPES, statuses=alerts.STATUSES,
                           threshold=cfg["ALERT_FAILED_THRESHOLD"], window=cfg["ALERT_WINDOW_MINUTES"])


@bp.post("/<int:alert_id>/acknowledge")
@role_required("warden")
def acknowledge(alert_id):
    outcome = alerts.acknowledge(get_db(), alert_id, g.user["id"], utc_now_text())
    messages = {"ok": ("Alert acknowledged.", "success"), "already": ("This alert was already acknowledged.", "info"),
                "missing": ("That alert does not exist.", "warning")}
    text, kind = messages[outcome]
    flash(text, kind)
    if outcome == "ok":
        current_app.logger.info("Alert acknowledged: alert_id=%s by warden_id=%s", alert_id, g.user["id"])
    if request.form.get("next") == "dashboard":
        return redirect(url_for("dashboards.warden_dashboard"))
    return redirect(url_for("alerts.index"))


# ---------------- student: emergency ----------------

@bp.get("/my")
@role_required("student")
def mine():
    return render_template("alerts/mine.html", title="Emergency alert", rows=alerts.for_student(get_db(), g.user["id"]),
                           cooldown=current_app.config["EMERGENCY_COOLDOWN_SECONDS"])


@bp.post("/emergency")
@role_required("student")
def emergency():
    db = get_db()
    # The alert is always for the logged-in student. The form carries nothing that names anyone.
    student = db.execute("SELECT * FROM student WHERE student_id = ?", (g.user["id"],)).fetchone()
    outcome, alert_id = alerts.create_emergency(db, student, current_app.config)
    if outcome == "created":
        emailer.kick(current_app._get_current_object())
        current_app.logger.warning("EMERGENCY alert raised: alert_id=%s student_id=%s", alert_id, g.user["id"])
        flash("Your emergency alert was sent to the warden. Stay where you are if it is safe. "
              "This does not call the police or any emergency service.", "danger")
    else:
        flash("An emergency alert from you was sent a moment ago and the warden has it. If you need help urgently, "
              "contact the warden directly.", "warning")
    return redirect(url_for("dashboards.student_dashboard") if request.form.get("next") == "dashboard" else url_for("alerts.mine"))