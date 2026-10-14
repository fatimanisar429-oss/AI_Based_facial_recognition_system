"""Sending email (SRS FR-13, FR-14, NFR-05).

How it works
  1. Whatever needs an email (an alert, a weekly report) first SAVES its own record, then adds rows to the email_outbox table.
  2. A background thread (and a scheduled job every few minutes) delivers the waiting rows through Flask-Mail.
  3. A row ends as Sent, Failed (will be retried, up to EMAIL_MAX_ATTEMPTS) or Skipped (no mail server is configured).
So a slow or broken mail server never delays a page and can never lose an alert. Passwords are never logged or stored in an error.
"""
import threading
from concurrent.futures import ThreadPoolExecutor

from flask_mail import Mail, Message

from ..db import connect
from ..timeutil import utc_now_text

mail = Mail()
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="email")
_send_lock = threading.Lock()                       # one sender at a time, so a message is never sent twice


def _is_placeholder(value):
    """.env.example used to ship 'smtp.example.com'. example.com is reserved for documentation, so it is never a real server."""
    host = (value or "").strip().lower().rsplit("@", 1)[-1].strip(" <>")
    return host == "example.com" or host.endswith(".example.com")


def smtp_configured(cfg):
    server, sender = cfg.get("MAIL_SERVER"), cfg.get("MAIL_DEFAULT_SENDER")
    return bool(server) and bool(sender) and not _is_placeholder(server)


def clean_error(error, cfg):
    """A short, safe description of a mail failure: no credentials, no long server banners."""
    text = f"{type(error).__name__}: {error}"
    for secret in (cfg.get("MAIL_PASSWORD"), cfg.get("MAIL_USERNAME")):
        if secret:
            text = text.replace(str(secret), "***")
    return " ".join(text.split())[:200]


# ---------- queueing ----------

def enqueue(db, kind, recipients, subject, body, related_id=None):
    """Add one outbox row per distinct recipient. Does NOT commit. Returns the number of rows added."""
    seen, added = set(), 0
    for address in recipients:
        address = (address or "").strip()
        if not address or address.lower() in seen:
            continue
        seen.add(address.lower())
        db.execute("INSERT INTO email_outbox (kind, recipient, subject, body, related_id, status, attempts, created_at) "
                   "VALUES (?, ?, ?, ?, ?, 'Pending', 0, ?)", (kind, address, subject, body, related_id, utc_now_text()))
        added += 1
    return added


# ---------- sending ----------

def _update_report_status(db, row, status):
    if row["kind"] == "weekly_report" and row["related_id"]:
        db.execute("UPDATE report SET email_status = ? WHERE report_id = ?",
                   ("Sent" if status == "Sent" else "Failed", row["related_id"]))


def send_pending(app, limit=50):
    """Deliver waiting messages. Returns a count dict. Safe to call from any thread; never raises for a mail problem."""
    counts = {"sent": 0, "failed": 0, "skipped": 0}
    if not _send_lock.acquire(blocking=False):
        return counts                               # another thread is already sending
    try:
        with app.app_context():
            db = connect(app)
            try:
                cfg = app.config
                rows = db.execute(
                    "SELECT * FROM email_outbox WHERE status = 'Pending' OR (status = 'Failed' AND attempts < ?) "
                    "ORDER BY email_id LIMIT ?", (cfg["EMAIL_MAX_ATTEMPTS"], limit)).fetchall()
                for row in rows:
                    if not smtp_configured(cfg):
                        db.execute("UPDATE email_outbox SET status = 'Skipped', last_error = ? WHERE email_id = ?",
                                   ("No mail server is configured (MAIL_SERVER / MAIL_DEFAULT_SENDER).", row["email_id"]))
                        _update_report_status(db, row, "Skipped")
                        counts["skipped"] += 1
                        continue
                    try:
                        mail.send(Message(subject=row["subject"], recipients=[row["recipient"]], body=row["body"], sender=cfg["MAIL_DEFAULT_SENDER"]))
                    except Exception as error:      # any mail problem: record it and carry on
                        message = clean_error(error, cfg)
                        db.execute("UPDATE email_outbox SET status = 'Failed', attempts = attempts + 1, last_error = ? WHERE email_id = ?",
                                   (message, row["email_id"]))
                        _update_report_status(db, row, "Failed")
                        app.logger.warning("Email %s to %s failed: %s", row["email_id"], row["recipient"], message)
                        counts["failed"] += 1
                    else:
                        db.execute("UPDATE email_outbox SET status = 'Sent', attempts = attempts + 1, last_error = NULL, sent_at = ? "
                                   "WHERE email_id = ?", (utc_now_text(), row["email_id"]))
                        _update_report_status(db, row, "Sent")
                        counts["sent"] += 1
                    db.commit()
                db.commit()
            finally:
                db.close()
    finally:
        _send_lock.release()
    return counts


def kick(app):
    """Start delivering in the background (or right now when EMAIL_SEND_ASYNC is off, which the tests use)."""
    if app.config["EMAIL_SEND_ASYNC"]:
        _executor.submit(send_pending, app)
    else:
        send_pending(app)


def send_test(app, recipient):
    """Send one message immediately and say what happened. Used by the admin's 'test email' button. Returns (ok, message)."""
    cfg = app.config
    if not smtp_configured(cfg):
        return False, "No mail server is configured. Set MAIL_SERVER and MAIL_DEFAULT_SENDER in .env and restart."
    try:
        with app.app_context():
            mail.send(Message(subject=f"Test email from {cfg['HOSTEL_NAME']}", recipients=[recipient], sender=cfg["MAIL_DEFAULT_SENDER"],
                              body="This is a test message. If you can read it, email is working."))
    except Exception as error:
        return False, "The test email could not be sent. " + clean_error(error, cfg)
    return True, f"A test email was sent to {recipient}."


def requeue_failed(db, kinds=("weekly_report",), days=28):
    """Give failed (or skipped) messages another full set of attempts, e.g. at the next weekly run."""
    from datetime import timedelta
    from ..timeutil import to_text, utc_now
    since = to_text(utc_now() - timedelta(days=days))
    marks = ",".join("?" * len(kinds))
    cur = db.execute(f"UPDATE email_outbox SET status = 'Pending', attempts = 0 WHERE status IN ('Failed', 'Skipped') "
                     f"AND kind IN ({marks}) AND created_at >= ?", (*kinds, since))
    return cur.rowcount