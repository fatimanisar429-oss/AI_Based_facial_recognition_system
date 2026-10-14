"""Entry log pages (SRS FR-08, UC-07). Read only: nothing here can change a log row."""
from flask import Blueprint, current_app, render_template, request

from ..db import get_db
from ..security import role_required
from ..services import entries
from ..timeutil import day_bounds
from . import filters

bp = Blueprint("logs", __name__, url_prefix="/logs")


@bp.get("/")
@role_required("admin", "warden")
def index():
    cfg = current_app.config
    first, last = filters.date_range(request.args, default_days=6)
    result_filter = request.args.get("result", "")
    method = request.args.get("method", "")
    review = request.args.get("review") == "1"
    q = request.args.get("q", "").strip()[:100]
    page = request.args.get("page", 1, type=int)
    start, end = day_bounds(first, last, cfg["TIMEZONE"], cfg["ATTENDANCE_DAY_START"])
    result = entries.search(get_db(), start, end, result_filter, method, review, q, page)
    args = {"from": first, "to": last, "result": result_filter, "method": method, "q": q}
    if review:
        args["review"] = "1"
    return render_template("logs/index.html", title="Entry logs", result=result, args=args, review=review)
