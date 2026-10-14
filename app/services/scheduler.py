"""Background jobs (APScheduler): the weekly parent report and the email queue.

Starting rules (so a development run does not end up with two schedulers):
  * never in tests, and never when SCHEDULER_ENABLED=0;
  * with Flask's debug reloader only in the child process that really serves requests (WERKZEUG_RUN_MAIN=true);
  * one scheduler per process, even if init_scheduler is called again.
If two processes did run it anyway, the weekly report is still created only once per student and period.
"""
import os
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from ..db import connect
from ..timeutil import hostel_today, utc_now_text
from . import emailer, reports

_scheduler = None


def run_weekly_reports(app):
    """The weekly job: re-queue last time's failures, create this week's reports, then deliver. Returns the report counts."""
    with app.app_context():
        db = connect(app)
        try:
            emailer.requeue_failed(db, kinds=("weekly_report",))
            result = reports.generate_weekly(db, app.config, hostel_today(app.config["TIMEZONE"], app.config["ATTENDANCE_DAY_START"]), utc_now_text())
        finally:
            db.close()
    emailer.send_pending(app)
    app.logger.info("Weekly reports: %s created, %s already existed (%s to %s)", result["created"], result["existing"], *result["period"])
    return result


def run_email_queue(app):
    return emailer.send_pending(app)


def build(app):
    """A scheduler with both jobs added but NOT started."""
    cfg = app.config
    hour, minute = cfg["WEEKLY_REPORT_TIME"].split(":")
    zone = ZoneInfo(cfg["TIMEZONE"])
    scheduler = BackgroundScheduler(timezone=zone)
    scheduler.add_job(run_weekly_reports, CronTrigger(day_of_week=cfg["WEEKLY_REPORT_DAY"], hour=int(hour), minute=int(minute), timezone=zone),
                      args=[app], id="weekly_reports", name="Weekly parent reports", max_instances=1, coalesce=True,
                      misfire_grace_time=6 * 3600, replace_existing=True)
    scheduler.add_job(run_email_queue, IntervalTrigger(minutes=5), args=[app], id="email_queue", name="Send queued email",
                      max_instances=1, coalesce=True, replace_existing=True)
    return scheduler


def should_start(app):
    if app.config.get("TESTING") or not app.config["SCHEDULER_ENABLED"]:
        return False
    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return False
    return True


def init_scheduler(app):
    """Start the scheduler once for this process. Returns it, or None when it must not run."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    if not should_start(app):
        return None
    _scheduler = build(app)
    _scheduler.start()
    return _scheduler