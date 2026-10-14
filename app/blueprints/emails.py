"""Email status for the administrator: is a mail server configured, send a test message, see what was sent or failed."""
from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from ..db import get_db
from ..security import role_required
from ..services import emailer
from ..services.people import EMAIL_PATTERN, paginate

bp = Blueprint("emails", __name__, url_prefix="/emails")

STATUSES = ("Pending", "Sent", "Failed", "Skipped")


@bp.get("/")
@role_required("admin")
def index():
    cfg = current_app.config
    status = request.args.get("status", "")
    status = status if status in STATUSES else ""
    page = request.args.get("page", 1, type=int)
    where, params = ("WHERE status = ?", [status]) if status else ("", [])
    db = get_db()
    result = paginate(db, f"SELECT COUNT(*) FROM email_outbox {where}",
                      f"SELECT * FROM email_outbox {where} ORDER BY email_id DESC", params, page)
    counts = {r["status"]: r["n"] for r in db.execute("SELECT status, COUNT(*) AS n FROM email_outbox GROUP BY status")}
    smtp = {"configured": emailer.smtp_configured(cfg), "server": cfg["MAIL_SERVER"], "port": cfg["MAIL_PORT"],
            "tls": cfg["MAIL_USE_TLS"], "sender": cfg["MAIL_DEFAULT_SENDER"], "login": bool(cfg["MAIL_USERNAME"])}   # never the password
    return render_template("emails/index.html", title="Email", result=result, status=status, statuses=STATUSES, counts=counts, smtp=smtp)


@bp.post("/test")
@role_required("admin")
def test():
    recipient = request.form.get("recipient", "").strip()
    if not recipient or len(recipient) > 100 or not EMAIL_PATTERN.match(recipient):
        flash("Enter a valid email address to send the test to.", "warning")
        return redirect(url_for("emails.index"))
    ok, message = emailer.send_test(current_app._get_current_object(), recipient)
    flash(message, "success" if ok else "danger")
    return redirect(url_for("emails.index"))


@bp.post("/retry")
@role_required("admin")
def retry():
    db = get_db()
    n = emailer.requeue_failed(db, kinds=("emergency", "security", "weekly_report", "test"), days=28)
    db.commit()
    emailer.kick(current_app._get_current_object())
    flash(f"{n} message(s) were queued to be sent again." if n else "There are no failed or skipped messages to retry.", "success" if n else "info")
    return redirect(url_for("emails.index"))