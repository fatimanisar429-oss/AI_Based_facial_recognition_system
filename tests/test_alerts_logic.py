"""Alerts, the email queue, weekly reports and the scheduler: rules and behaviour without a browser."""
import smtplib
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from app import create_app
from app.db import get_db
from app.services import alerts, emailer, reports, scheduler
from app.timeutil import to_text, utc_now

PASSWORD = "S3cret-mail-pw"


def when(minutes=0.0):
    return utc_now() - timedelta(minutes=minutes)


def log_denied(app, code, minutes_ago=0.0, result="Denied"):
    with app.app_context():
        get_db().execute(
            "INSERT INTO entry_log (attempted_at, method, result, door_status, failure_code) VALUES (?, 'None', ?, ?, ?)",
            (to_text(when(minutes_ago)), result, "Locked" if result == "Denied" else "Unlocked", code if result == "Denied" else None))
        get_db().commit()


def check(app, now=None, **cfg):
    with app.app_context():
        config = {**app.config, **cfg}
        alert_id = alerts.check_failed_attempts(get_db(), config, now)
        get_db().commit()
        return alert_id


def all_alerts(app, kind="Unauthorized Attempt"):
    with app.app_context():
        return get_db().execute("SELECT * FROM alert WHERE alert_type = ? ORDER BY alert_id", (kind,)).fetchall()


def outbox(app, kind=None):
    with app.app_context():
        sql, params = "SELECT * FROM email_outbox", ()
        if kind:
            sql, params = sql + " WHERE kind = ?", (kind,)
        return get_db().execute(sql + " ORDER BY email_id", params).fetchall()


@pytest.fixture
def mailapp(app):
    app.config.update(MAIL_SERVER="smtp.test", MAIL_PORT=587, MAIL_DEFAULT_SENDER="Hostel <hostel@test.local>", MAIL_PASSWORD=PASSWORD, MAIL_USERNAME="hostel-user")
    return app


# ================= automatic alert: the three-failures rule =================

def test_two_failures_are_not_enough(app, users):
    for _ in range(2):
        log_denied(app, "not_recognized", 1)
    assert check(app) is None and all_alerts(app) == []


def test_three_failures_in_five_minutes_raise_one_alert(app, users):
    for code in ("not_recognized", "liveness_failed", "not_recognized"):
        log_denied(app, code, 1)
    alert_id = check(app)
    (a,) = all_alerts(app)
    assert alert_id == a["alert_id"] and a["status"] == "New" and a["student_id"] is None and a["alert_type"] == "Unauthorized Attempt"
    assert "3 failed entry attempts" in a["message"] and "within 5 minutes" in a["message"] and "no student is named" in a["message"]


def test_the_window_edges(app, users):
    log_denied(app, "not_recognized", 4.9)
    log_denied(app, "not_recognized", 4.9)
    log_denied(app, "not_recognized", 5.1)               # just too old
    assert check(app) is None
    log_denied(app, "not_recognized", 0.1)
    assert check(app) is not None                         # now three are inside the window


def test_old_failures_never_count(app, users):
    for _ in range(10):
        log_denied(app, "not_recognized", 60)
    assert check(app) is None


@pytest.mark.parametrize("code", ["no_face", "too_dark", "too_bright", "blurry", "face_too_small", "bad_frames", "multiple_faces",
                                  "no_enrolled", "processing_error", "guest_used", "guest_expired", "guest_pending", "guest_no_visit"])
def test_technical_and_rule_failures_are_not_counted(app, users, code):
    for _ in range(6):
        log_denied(app, code, 1)
    assert check(app) is None


def test_the_three_counted_kinds_all_count(app, users):
    for code in ("liveness_failed", "not_recognized", "ambiguous"):
        log_denied(app, code, 1)
    assert check(app) is not None


def test_a_success_in_between_does_not_reset_the_count(app, users):
    log_denied(app, "not_recognized", 4)
    log_denied(app, None, 3, result="Granted")
    log_denied(app, "not_recognized", 2)
    log_denied(app, "liveness_failed", 1)
    assert check(app) is not None


def test_the_same_incident_raises_only_one_alert(app, users):
    for _ in range(3):
        log_denied(app, "not_recognized", 1)
    assert check(app) is not None
    for _ in range(5):                                      # the attack goes on
        log_denied(app, "not_recognized", 0.5)
        assert check(app) is None
    assert len(all_alerts(app)) == 1


def test_a_new_incident_after_the_window_raises_a_new_alert(app, users):
    for _ in range(3):
        log_denied(app, "not_recognized", 1)
    check(app)
    later = when(-6)                                         # six minutes from now
    with app.app_context():
        for _ in range(3):
            get_db().execute("INSERT INTO entry_log (attempted_at, method, result, door_status, failure_code) VALUES (?, 'None', 'Denied', 'Locked', 'not_recognized')",
                             (to_text(later - timedelta(minutes=1)),))
        get_db().commit()
    assert check(app, now=later) is not None and len(all_alerts(app)) == 2


def test_old_failures_alone_do_not_start_a_second_incident(app, users):
    for _ in range(3):
        log_denied(app, "not_recognized", 1)
    check(app)
    assert check(app, now=when(-10)) is None                 # ten minutes later, nothing new happened


def test_threshold_and_window_are_configurable(app, users):
    log_denied(app, "not_recognized", 1)
    log_denied(app, "not_recognized", 8)
    assert check(app, ALERT_FAILED_THRESHOLD=2, ALERT_WINDOW_MINUTES=10) is not None
    assert "within 10 minutes" in all_alerts(app)[0]["message"]


def test_attempts_are_never_attributed_to_a_student(app, users):
    with app.app_context():
        get_db().execute("INSERT INTO entry_log (subject_type, student_id, attempted_at, method, result, door_status) VALUES ('student',1,?, 'Face','Granted','Unlocked')", (to_text(when(1)),))
        get_db().commit()
    for _ in range(3):
        log_denied(app, "not_recognized", 1)
    check(app)
    assert all_alerts(app)[0]["student_id"] is None


# ---- who is emailed about a security alert ----

def add_contacts(app):
    with app.app_context():
        db = get_db()
        for name, kind, email in (("Security Office", "Security", "security@x.com"), ("Police", "Police", "police@x.com"), ("Other", "Other", "other@x.com"),
                                  ("Dup", "Security", "SECURITY@x.com")):
            db.execute("INSERT INTO emergency_contact (contact_name, contact_type, phone, email, admin_id) VALUES (?, ?, '1234567', ?, 1)", (name, kind, email))
        db.commit()


def test_security_alert_goes_to_wardens_and_security_contacts_only(app, users):
    add_contacts(app)
    with app.app_context():
        get_db().execute("INSERT INTO warden (full_name,email,phone,gender,password_hash,is_active) VALUES ('Gone','gone@x.com','1','F','h',0)")
        get_db().commit()
    for _ in range(3):
        log_denied(app, "not_recognized", 1)
    check(app)
    recipients = sorted(r["recipient"] for r in outbox(app, "security"))
    assert recipients == ["security@x.com", "warden@x.com"]          # no police/other, no inactive warden, no duplicate
    mail = outbox(app, "security")[0]
    assert "Security alert" in mail["subject"] and "no student is named" in mail["body"] and mail["status"] == "Pending"


# ================= emergency alerts =================

def student_row(app, sid=1):
    with app.app_context():
        return get_db().execute("SELECT * FROM student WHERE student_id = ?", (sid,)).fetchone()


def press(app, sid=1, now=None):
    with app.app_context():
        return alerts.create_emergency(get_db(), student_row(app, sid), app.config, now)


def test_emergency_saves_an_alert_for_that_student(app, users):
    outcome, alert_id = press(app, 1)
    (a,) = all_alerts(app, "Emergency")
    assert outcome == "created" and a["alert_id"] == alert_id and a["student_id"] == 1 and a["status"] == "New"
    assert "Student 1" in a["message"] and "Room 1" in a["message"]


def test_emergency_emails_wardens_and_all_contacts(app, users):
    add_contacts(app)
    press(app, 1)
    recipients = sorted(r["recipient"] for r in outbox(app, "emergency"))
    assert recipients == ["other@x.com", "police@x.com", "security@x.com", "warden@x.com"]
    body = outbox(app, "emergency")[0]["body"]
    assert "Student 1" in body and "REG-001" in body and "Room: 1" in body and "Phone:" in body and "Time:" in body


def test_emergency_text_never_claims_to_call_anyone(app, users):
    press(app, 1)
    body = outbox(app, "emergency")[0]["body"].lower()
    assert "called the police" not in body and "police have been" not in body and "ambulance" not in body


def test_a_second_press_inside_the_cooldown_is_not_a_new_alert(app, users):
    first = press(app, 1)
    second = press(app, 1)
    assert first[0] == "created" and second == ("cooldown", first[1]) and len(all_alerts(app, "Emergency")) == 1
    assert len(outbox(app, "emergency")) == 1                # and no second email either


def test_after_the_cooldown_a_new_alert_is_possible(app, users):
    press(app, 1)
    assert press(app, 1, now=utc_now() + timedelta(seconds=app.config["EMERGENCY_COOLDOWN_SECONDS"] + 5))[0] == "created"


def test_two_students_do_not_block_each_other(app, users):
    assert press(app, 1)[0] == "created" and press(app, 2)[0] == "created"


def test_emergency_rolls_back_completely_if_the_email_queue_fails(app, users, monkeypatch):
    monkeypatch.setattr(emailer, "enqueue", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("queue broken")))
    with pytest.raises(RuntimeError):
        press(app, 1)
    assert all_alerts(app, "Emergency") == []


# ================= acknowledging =================

def test_acknowledge_records_who_and_when(app, users):
    _, alert_id = press(app, 1)
    with app.app_context():
        assert alerts.acknowledge(get_db(), alert_id, 1, "2026-10-14T10:00:00") == "ok"
        row = get_db().execute("SELECT * FROM alert WHERE alert_id = ?", (alert_id,)).fetchone()
    assert (row["status"], row["acknowledged_by_warden_id"], row["acknowledged_at"]) == ("Acknowledged", 1, "2026-10-14T10:00:00")


def test_an_alert_is_acknowledged_once(app, users):
    _, alert_id = press(app, 1)
    with app.app_context():
        db = get_db()
        assert alerts.acknowledge(db, alert_id, 1, "2026-10-14T10:00:00") == "ok"
        assert alerts.acknowledge(db, alert_id, 1, "2026-10-14T11:00:00") == "already"
        assert db.execute("SELECT acknowledged_at FROM alert").fetchone()[0] == "2026-10-14T10:00:00"      # the first time stays
        assert alerts.acknowledge(db, 999, 1, "x") == "missing"
        assert alerts.new_count(db) == 0


# ================= the email queue =================

def test_enqueue_removes_duplicates_and_blanks_and_does_not_commit(app, users):
    with app.app_context():
        db = get_db()
        n = emailer.enqueue(db, "test", ["a@x.com", "A@X.com", "", None, " b@x.com "], "s", "b")
        db.rollback()
        assert n == 2 and db.execute("SELECT COUNT(*) FROM email_outbox").fetchone()[0] == 0


def queue(app, n=1, kind="emergency"):
    with app.app_context():
        emailer.enqueue(get_db(), kind, [f"r{i}@x.com" for i in range(n)], "Subject", "Body text")
        get_db().commit()


def test_without_a_mail_server_messages_are_skipped_not_lost(app, users):
    queue(app, 2)
    assert emailer.send_pending(app) == {"sent": 0, "failed": 0, "skipped": 2}
    rows = outbox(app)
    assert {r["status"] for r in rows} == {"Skipped"} and "No mail server" in rows[0]["last_error"]


def test_messages_are_sent_with_the_configured_sender(mailapp, users):
    queue(mailapp, 2)
    with emailer.mail.record_messages() as sent:
        assert emailer.send_pending(mailapp)["sent"] == 2
    assert [m.recipients for m in sent] == [["r0@x.com"], ["r1@x.com"]] and sent[0].sender == "Hostel <hostel@test.local>"
    assert sent[0].subject == "Subject" and sent[0].body == "Body text"
    rows = outbox(mailapp)
    assert {r["status"] for r in rows} == {"Sent"} and all(r["sent_at"] and r["attempts"] == 1 and r["last_error"] is None for r in rows)


def fail_send(monkeypatch, error=None):
    def boom(message):
        raise error or smtplib.SMTPServerDisconnected("connection lost")
    monkeypatch.setattr(emailer.mail, "send", boom)


def test_a_mail_failure_is_recorded_and_never_raises(mailapp, users, monkeypatch):
    queue(mailapp)
    fail_send(monkeypatch)
    assert emailer.send_pending(mailapp) == {"sent": 0, "failed": 1, "skipped": 0}
    (r,) = outbox(mailapp)
    assert r["status"] == "Failed" and r["attempts"] == 1 and "SMTPServerDisconnected" in r["last_error"]


def test_failed_messages_are_retried_until_the_limit(mailapp, users, monkeypatch):
    queue(mailapp)
    fail_send(monkeypatch)
    for _ in range(8):
        emailer.send_pending(mailapp)
    (r,) = outbox(mailapp)
    assert r["status"] == "Failed" and r["attempts"] == mailapp.config["EMAIL_MAX_ATTEMPTS"]


def test_a_retry_that_works_clears_the_error(mailapp, users, monkeypatch):
    queue(mailapp)
    with monkeypatch.context() as m:
        fail_send(m)
        emailer.send_pending(mailapp)
    with emailer.mail.record_messages() as sent:
        emailer.send_pending(mailapp)
    (r,) = outbox(mailapp)
    assert len(sent) == 1 and r["status"] == "Sent" and r["attempts"] == 2 and r["last_error"] is None


def test_one_bad_message_does_not_stop_the_others(mailapp, users, monkeypatch):
    queue(mailapp, 3)
    calls = {"n": 0}
    real = emailer.mail.send

    def flaky(message):
        calls["n"] += 1
        if calls["n"] == 2:
            raise smtplib.SMTPRecipientsRefused({})
        return real(message)
    monkeypatch.setattr(emailer.mail, "send", flaky)
    assert emailer.send_pending(mailapp) == {"sent": 2, "failed": 1, "skipped": 0}


def test_credentials_never_appear_in_stored_errors(mailapp, users, monkeypatch):
    queue(mailapp)
    fail_send(monkeypatch, smtplib.SMTPAuthenticationError(535, f"bad login hostel-user / {PASSWORD}".encode()))
    emailer.send_pending(mailapp)
    error = outbox(mailapp)[0]["last_error"]
    assert PASSWORD not in error and "hostel-user" not in error and "***" in error and len(error) <= 200


def test_clean_error_truncates_long_text(mailapp):
    assert len(emailer.clean_error(Exception("x" * 1000), mailapp.config)) <= 200


def test_two_senders_never_run_at_once(mailapp, users):
    queue(mailapp)
    assert emailer._send_lock.acquire(blocking=False)
    try:
        assert emailer.send_pending(mailapp) == {"sent": 0, "failed": 0, "skipped": 0}
    finally:
        emailer._send_lock.release()
    assert outbox(mailapp)[0]["status"] == "Pending"


def test_requeue_gives_failed_and_skipped_messages_a_fresh_start(mailapp, users, monkeypatch):
    queue(mailapp, 2, "weekly_report")
    fail_send(monkeypatch)
    for _ in range(6):
        emailer.send_pending(mailapp)
    monkeypatch.undo()
    with mailapp.app_context():
        assert emailer.requeue_failed(get_db()) == 2
        get_db().commit()
    assert {r["status"] for r in outbox(mailapp)} == {"Pending"} and {r["attempts"] for r in outbox(mailapp)} == {0}
    with emailer.mail.record_messages() as sent:
        emailer.send_pending(mailapp)
    assert len(sent) == 2


def test_requeue_only_touches_the_kinds_asked_for(mailapp, users):
    queue(mailapp, 1, "emergency")
    emailer.send_pending(create_unconfigured(mailapp))
    with mailapp.app_context():
        assert emailer.requeue_failed(get_db(), kinds=("weekly_report",)) == 0


def create_unconfigured(app):
    app.config["MAIL_SERVER"] = ""
    return app


def test_sending_in_the_background_does_not_block(tmp_path, users):
    app = create_app({"TESTING": True, "SECRET_KEY": "k", "DATABASE_PATH": str(tmp_path / "bg.db"), "EMAIL_SEND_ASYNC": True,
                      "MAIL_SERVER": "smtp.test", "MAIL_DEFAULT_SENDER": "h@test.local", "SCHEDULER_ENABLED": False})
    queue(app)
    gate = threading.Event()
    original = emailer.mail.send
    emailer.mail.send = lambda message: (gate.wait(3), original(message))[1]        # a very slow mail server
    try:
        started = time.perf_counter()
        emailer.kick(app)
        assert time.perf_counter() - started < 0.5                                  # the caller is not held up
        assert outbox(app)[0]["status"] == "Pending"
        gate.set()
        for _ in range(50):
            if outbox(app)[0]["status"] == "Sent":
                break
            time.sleep(0.1)
        assert outbox(app)[0]["status"] == "Sent"
    finally:
        emailer.mail.send = original
        gate.set()


# ---- the test email ----

def test_test_email_without_a_server(app):
    ok, message = emailer.send_test(app, "me@x.com")
    assert not ok and "No mail server" in message


def test_test_email_with_a_server(mailapp):
    with emailer.mail.record_messages() as sent:
        ok, message = emailer.send_test(mailapp, "me@x.com")
    assert ok and "me@x.com" in message and sent[0].recipients == ["me@x.com"] and "Test email" in sent[0].subject


def test_test_email_failure_is_explained_without_the_password(mailapp, monkeypatch):
    fail_send(monkeypatch, smtplib.SMTPAuthenticationError(535, f"rejected {PASSWORD}".encode()))
    ok, message = emailer.send_test(mailapp, "me@x.com")
    assert not ok and "could not be sent" in message and PASSWORD not in message


# ================= weekly report =================

def test_the_report_period_is_the_seven_complete_days_before_today():
    assert reports.previous_week("2026-10-18") == ("2026-10-11", "2026-10-17")
    assert reports.previous_week("2026-03-01") == ("2026-02-22", "2026-02-28")


def add_attendance(app, student_id, days, status="Present"):
    with app.app_context():
        for d in days:
            get_db().execute("INSERT INTO attendance (student_id, local_date, local_time, status) VALUES (?,?,?,?)", (student_id, d, "20:00:00", status))
        get_db().commit()


def stats(app, sid, first="2026-10-11", last="2026-10-17"):
    with app.app_context():
        return reports.student_period_stats(get_db(), sid, first, last)


def test_period_statistics(app, users):
    add_attendance(app, 1, ["2026-10-11", "2026-10-12", "2026-10-13", "2026-10-14"])
    add_attendance(app, 1, ["2026-10-15"], "Late")
    add_attendance(app, 1, ["2026-10-10", "2026-10-18"])                         # outside the period
    s = stats(app, 1)
    assert s == {"days": 7, "days_present": 5, "late_entries": 1, "days_absent": 2, "percentage": 71.4}


def test_nobody_present_and_everyone_present(app, users):
    assert stats(app, 2)["percentage"] == 0.0 and stats(app, 2)["days_absent"] == 7
    add_attendance(app, 2, [f"2026-10-{d}" for d in range(11, 18)])
    assert stats(app, 2)["percentage"] == 100.0


def generate(app, today="2026-10-18"):
    with app.app_context():
        return reports.generate_weekly(get_db(), app.config, today, "2026-10-18T15:00:00")


def test_weekly_reports_are_created_for_active_students_with_a_parent_email(app, users):
    add_attendance(app, 1, ["2026-10-12", "2026-10-13"])
    with app.app_context():
        get_db().execute("UPDATE student SET is_active = 0 WHERE student_id = 2")
        get_db().commit()
    result = generate(app)
    assert result["created"] == 1 and result["period"] == ("2026-10-11", "2026-10-17")
    with app.app_context():
        r = get_db().execute("SELECT * FROM report").fetchone()
    assert (r["student_id"], r["days_present"], r["late_entries"], r["attendance_percentage"], r["email_status"], r["generated_date"]) == \
        (1, 2, 0, 28.6, "Pending", "2026-10-18")
    (mail,) = outbox(app, "weekly_report")
    assert mail["recipient"] == "p@x.com" and mail["related_id"] == r["report_id"] and "Student 1" in mail["subject"]
    for text in ("Dear P", "2026-10-11 to 2026-10-17", "2 of 7", "28.6%", "Days absent: 5"):
        assert text in mail["body"]


def test_running_the_job_twice_creates_each_report_once(app, users):
    assert generate(app)["created"] == 2
    again = generate(app)
    assert again == {"created": 0, "existing": 2, "period": ("2026-10-11", "2026-10-17")}
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM report").fetchone()[0] == 2
    assert len(outbox(app, "weekly_report")) == 2


def test_the_database_refuses_a_duplicate_report(app, users):
    generate(app)
    with app.app_context(), pytest.raises(Exception):
        get_db().execute("INSERT INTO report (student_id, period_start, period_end, days_present, late_entries, attendance_percentage, email_status, generated_date) "
                         "VALUES (1, '2026-10-11', '2026-10-17', 0, 0, 0, 'Pending', '2026-10-18')")


def test_a_new_week_gets_new_reports(app, users):
    generate(app, "2026-10-18")
    assert generate(app, "2026-10-25")["created"] == 2


def test_weekly_job_sends_and_marks_reports_sent(mailapp, users, monkeypatch):
    monkeypatch.setattr(scheduler, "hostel_today", lambda tz, start: "2026-10-18")
    with emailer.mail.record_messages() as sent:
        result = scheduler.run_weekly_reports(mailapp)
    assert result["created"] == 2 and len(sent) == 2
    with mailapp.app_context():
        assert {r[0] for r in get_db().execute("SELECT email_status FROM report")} == {"Sent"}


def test_weekly_job_without_a_mail_server_keeps_the_reports_and_marks_them_failed(app, users, monkeypatch):
    monkeypatch.setattr(scheduler, "hostel_today", lambda tz, start: "2026-10-18")
    scheduler.run_weekly_reports(app)
    with app.app_context():
        assert {r[0] for r in get_db().execute("SELECT email_status FROM report")} == {"Failed"}
        assert get_db().execute("SELECT COUNT(*) FROM report").fetchone()[0] == 2                 # nothing was lost
    assert {r["status"] for r in outbox(app)} == {"Skipped"}


def test_the_next_weekly_run_retries_last_weeks_failures(app, users, monkeypatch):
    monkeypatch.setattr(scheduler, "hostel_today", lambda tz, start: "2026-10-18")
    scheduler.run_weekly_reports(app)                                                            # no mail server yet
    app.config.update(MAIL_SERVER="smtp.test", MAIL_DEFAULT_SENDER="h@test.local")               # the administrator fixes it
    with emailer.mail.record_messages() as sent:
        scheduler.run_weekly_reports(app)
    assert len(sent) == 2
    with app.app_context():
        assert {r[0] for r in get_db().execute("SELECT email_status FROM report")} == {"Sent"}
        assert get_db().execute("SELECT COUNT(*) FROM report").fetchone()[0] == 2                 # still one per student


def test_a_failed_weekly_email_is_marked_failed_on_the_report(mailapp, users, monkeypatch):
    monkeypatch.setattr(scheduler, "hostel_today", lambda tz, start: "2026-10-18")
    fail_send(monkeypatch)
    scheduler.run_weekly_reports(mailapp)
    with mailapp.app_context():
        assert {r[0] for r in get_db().execute("SELECT email_status FROM report")} == {"Failed"}


# ================= the scheduler =================

def test_the_scheduler_has_the_weekly_job_and_the_queue_job(app):
    s = scheduler.build(app)
    assert not s.running
    jobs = {j.id: j for j in s.get_jobs()}
    assert set(jobs) == {"weekly_reports", "email_queue"}
    weekly = {f.name: str(f) for f in jobs["weekly_reports"].trigger.fields}
    assert (weekly["day_of_week"], weekly["hour"], weekly["minute"]) == ("sun", "20", "0")
    assert str(jobs["weekly_reports"].trigger.timezone) == "Asia/Karachi"
    assert jobs["email_queue"].trigger.interval == timedelta(minutes=5)
    for job in jobs.values():
        assert job.max_instances == 1 and job.coalesce is True


def test_the_weekly_schedule_is_configurable(tmp_path):
    app = create_app({"TESTING": True, "SECRET_KEY": "k", "DATABASE_PATH": str(tmp_path / "s.db"), "WEEKLY_REPORT_DAY": "fri", "WEEKLY_REPORT_TIME": "18:30"})
    weekly = {f.name: str(f) for f in {j.id: j for j in scheduler.build(app).get_jobs()}["weekly_reports"].trigger.fields}
    assert (weekly["day_of_week"], weekly["hour"], weekly["minute"]) == ("fri", "18", "30")


@pytest.fixture
def fresh_scheduler():
    scheduler._scheduler = None
    yield
    if scheduler._scheduler is not None:
        scheduler._scheduler.shutdown(wait=False)
    scheduler._scheduler = None


def live_app(tmp_path, **cfg):
    return create_app({"TESTING": False, "DEBUG": False, "SECRET_KEY": "a-real-looking-secret-key", "DATABASE_PATH": str(tmp_path / "l.db"),
                       "SCHEDULER_ENABLED": True, **cfg})


def test_no_scheduler_in_tests(app, fresh_scheduler):
    assert scheduler.init_scheduler(app) is None


def test_the_scheduler_can_be_switched_off(tmp_path, fresh_scheduler):
    assert scheduler.init_scheduler(live_app(tmp_path, SCHEDULER_ENABLED=False)) is None


def test_the_debug_reloader_parent_does_not_start_a_scheduler(tmp_path, fresh_scheduler, monkeypatch):
    monkeypatch.delenv("WERKZEUG_RUN_MAIN", raising=False)
    assert scheduler.init_scheduler(live_app(tmp_path, DEBUG=True)) is None
    assert scheduler._scheduler is None


def test_the_serving_process_starts_exactly_one_scheduler(tmp_path, fresh_scheduler, monkeypatch):
    monkeypatch.setenv("WERKZEUG_RUN_MAIN", "true")
    app = live_app(tmp_path, DEBUG=True)
    first = scheduler._scheduler                         # create_app already started it for this process
    assert first is not None and first.running
    assert scheduler.init_scheduler(app) is first and scheduler.init_scheduler(app) is first       # asking again changes nothing


def test_a_normal_run_starts_one_scheduler(tmp_path, fresh_scheduler):
    app = live_app(tmp_path)
    first = scheduler._scheduler
    assert first is not None and first.running and {j.id for j in first.get_jobs()} == {"weekly_reports", "email_queue"}
    assert scheduler.init_scheduler(app) is first


# ================= command line =================

def test_send_test_email_command(app, mailapp):
    runner = mailapp.test_cli_runner()
    with emailer.mail.record_messages() as sent:
        result = runner.invoke(args=["send-test-email", "--to", "me@x.com"])
    assert result.exit_code == 0 and "me@x.com" in result.output and len(sent) == 1


def test_send_test_email_command_reports_a_missing_server(app):
    result = app.test_cli_runner().invoke(args=["send-test-email", "--to", "me@x.com"])
    assert result.exit_code != 0 and "No mail server" in result.output


def test_send_weekly_reports_command(app, users, monkeypatch):
    monkeypatch.setattr(scheduler, "hostel_today", lambda tz, start: "2026-10-18")
    result = app.test_cli_runner().invoke(args=["send-weekly-reports"])
    assert result.exit_code == 0 and "2026-10-11 to 2026-10-17" in result.output and "2 created" in result.output

# ================= placeholder mail settings are not a real server =================

@pytest.mark.parametrize("server,sender,expected", [
    ("smtp.gmail.com", "hostel@gmail.com", True), ("127.0.0.1", "Hostel <h@test.local>", True),
    ("smtp.example.com", "hostel-security@example.com", False), ("example.com", "x@y.com", False), ("SMTP.EXAMPLE.COM", "x@y.com", False),
    ("", "x@y.com", False), ("smtp.gmail.com", "", False), ("", "", False), (None, None, False),
])
def test_placeholder_or_missing_mail_settings_count_as_not_configured(server, sender, expected):
    assert emailer.smtp_configured({"MAIL_SERVER": server, "MAIL_DEFAULT_SENDER": sender}) is expected


def test_the_example_env_file_ships_without_a_mail_server():
    import pathlib
    text = (pathlib.Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
    assert "MAIL_SERVER=\n" in text and "example.com" not in text.split("# --- Email")[1].split("BIOMETRIC_KEY")[0]
