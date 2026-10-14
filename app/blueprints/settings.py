"""Basic system settings (admin only). Today: the curfew time that decides Present or Late."""
from flask import Blueprint, current_app, flash, g, redirect, render_template, request, url_for

from ..db import get_db
from ..security import role_required
from ..services import appsettings

bp = Blueprint("settings", __name__, url_prefix="/settings")


@bp.route("/", methods=["GET", "POST"])
@role_required("admin")
def index():
    cfg, db = current_app.config, get_db()
    error, typed = None, appsettings.curfew_time(db, cfg)
    if request.method == "POST":
        typed = request.form.get("curfew_time", "").strip()
        error = appsettings.validate_curfew(typed, cfg["ATTENDANCE_DAY_START"])
        if error is None:
            appsettings.put(db, "curfew_time", typed)
            current_app.logger.info("Curfew changed to %s by admin_id=%s", typed, g.user["id"])
            flash(f"Curfew saved: {typed}. It applies to entries from now on; existing attendance is not changed.", "success")
            return redirect(url_for("settings.index"))
    return render_template("settings/index.html", title="Settings", curfew=typed, error=error, cfg=cfg), (400 if error else 200)
