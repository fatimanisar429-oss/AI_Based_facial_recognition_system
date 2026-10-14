"""Attendance rules: first entry of the hostel day, Present/Late by curfew, no duplicates, nothing for failures."""
import io
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app import create_app
from app.db import get_db
from app.services import appsettings, attendance, entries
from app.services import verification as V
from app.timeutil import hostel_day, hostel_today
from conftest import login, token_when_logged_in

KARACHI = ZoneInfo("Asia/Karachi")          # UTC+5, no daylight saving


def at(local_text):
    """'2026-10-11 21:59' in Karachi -> an aware UTC datetime."""
    return datetime.strptime(local_text, "%Y-%m-%d %H:%M").replace(tzinfo=KARACHI).astimezone(timezone.utc)


def mark(app, student_id, local_text):
    with app.app_context():
        db = get_db()
        info = attendance.mark_entry(db, student_id, at(local_text), app.config)
        db.commit()
        return info


def records(app, student_id=None):
    with app.app_context():
        sql, params = "SELECT * FROM attendance", ()
        if student_id:
            sql, params = sql + " WHERE student_id = ?", (student_id,)
        return get_db().execute(sql + " ORDER BY local_date", params).fetchall()


# ================= Present or Late =================

@pytest.mark.parametrize("local,status", [
    ("2026-10-11 06:00", "Present"),
    ("2026-10-11 21:59", "Present"),
    ("2026-10-11 22:00", "Present"),          # exactly at the curfew is not after it
    ("2026-10-11 22:01", "Late"),
    ("2026-10-11 23:59", "Late"),
])
def test_status_depends_on_the_curfew(app, users, local, status):
    info = mark(app, 1, local)
    assert info["status"] == status and info["first_entry"] and info["date"] == "2026-10-11"
    assert records(app)[0]["status"] == status and records(app)[0]["local_date"] == "2026-10-11"


@pytest.mark.parametrize("local,hostel_date", [
    ("2026-10-12 00:00", "2026-10-11"), ("2026-10-12 00:30", "2026-10-11"), ("2026-10-12 03:59", "2026-10-11"),
    ("2026-10-12 04:00", "2026-10-12"), ("2026-10-12 04:01", "2026-10-12"),
])
def test_arrivals_after_midnight_belong_to_the_previous_day(app, users, local, hostel_date):
    info = mark(app, 1, local)
    assert info["date"] == hostel_date


def test_arrival_at_half_past_midnight_is_late_for_yesterday(app, users):
    info = mark(app, 1, "2026-10-12 00:30")
    assert info == {"status": "Late", "first_entry": True, "date": "2026-10-11"}


def test_early_morning_after_the_day_start_is_a_new_day_and_present(app, users):
    assert mark(app, 1, "2026-10-12 04:05") == {"status": "Present", "first_entry": True, "date": "2026-10-12"}


# ================= one record per day =================

def test_later_entries_the_same_day_do_not_create_another_record(app, users):
    first = mark(app, 1, "2026-10-11 18:00")
    second = mark(app, 1, "2026-10-11 20:00")
    third = mark(app, 1, "2026-10-11 23:30")                      # after curfew, but the day already has its record
    assert first["first_entry"] and not second["first_entry"] and not third["first_entry"]
    assert third["status"] == "Present"                            # the first entry decided; it is never changed
    rows = records(app, 1)
    assert len(rows) == 1 and rows[0]["local_time"] == "18:00:00" and rows[0]["status"] == "Present"


def test_a_late_first_entry_stays_late(app, users):
    mark(app, 1, "2026-10-11 22:30")
    assert mark(app, 1, "2026-10-11 22:45")["status"] == "Late" and len(records(app)) == 1


def test_each_student_and_each_day_gets_its_own_record(app, users):
    mark(app, 1, "2026-10-11 18:00")
    mark(app, 2, "2026-10-11 18:05")
    mark(app, 1, "2026-10-12 18:00")
    assert len(records(app)) == 3 and len(records(app, 1)) == 2


def test_the_database_itself_refuses_a_second_record_for_the_same_day(app, users):
    mark(app, 1, "2026-10-11 18:00")
    with app.app_context():
        with pytest.raises(sqlite3.IntegrityError):
            get_db().execute("INSERT INTO attendance (student_id, local_date, local_time, status) "
                             "VALUES (1, '2026-10-11', '19:00:00', 'Present')")


# ================= configuration =================

def test_saved_curfew_overrides_the_default(app, users):
    with app.app_context():
        appsettings.put(get_db(), "curfew_time", "20:00")
    assert mark(app, 1, "2026-10-11 20:30")["status"] == "Late"
    assert mark(app, 2, "2026-10-11 19:59")["status"] == "Present"


def test_invalid_saved_curfew_falls_back_to_the_env_value(app, users):
    with app.app_context():
        get_db().execute("INSERT INTO setting (key, value) VALUES ('curfew_time', 'banana')")
        get_db().commit()
        assert appsettings.curfew_time(get_db(), app.config) == "22:00"


def app_in(tmp_path, tz):
    app = create_app({"TESTING": True, "SECRET_KEY": "k", "DATABASE_PATH": str(tmp_path / f"{tz.replace('/', '_')}.db"),
                      "TIMEZONE": tz, "CURFEW_TIME": "22:00", "ATTENDANCE_DAY_START": "04:00"})
    with app.app_context():
        get_db().execute("INSERT INTO student (registration_no,full_name,email,phone,room_no,department,session,gender,parent_name,"
                         "parent_email,password_hash) VALUES ('REG-1','S','s@x.com','1','1','CS','2022','F','P','p@x.com','h')")
        get_db().commit()
    return app


def test_timezone_is_configurable(tmp_path):
    """The same instant (21:59 UTC) is 21:59 in UTC but 02:59 the next morning in Karachi."""
    moment = datetime(2026, 10, 11, 21, 59, tzinfo=timezone.utc)
    results = {}
    for tz in ("UTC", "Asia/Karachi"):
        app = app_in(tmp_path, tz)
        with app.app_context():
            results[tz] = attendance.mark_entry(get_db(), 1, moment, app.config)
    assert results["UTC"] == {"status": "Present", "first_entry": True, "date": "2026-10-11"}     # before the 22:00 curfew
    assert results["Asia/Karachi"] == {"status": "Late", "first_entry": True, "date": "2026-10-11"}  # 02:59, late for the 11th


def test_hostel_today_uses_the_day_start(monkeypatch):
    import app.timeutil as tu
    monkeypatch.setattr(tu, "utc_now", lambda: datetime(2026, 10, 11, 20, 30, tzinfo=timezone.utc))   # 01:30 Karachi on the 12th
    assert tu.hostel_today("Asia/Karachi", "04:00") == "2026-10-11"
    assert tu.hostel_today("Asia/Karachi", "00:00") == "2026-10-12"


# ================= from the entry terminal =================

def gate(app, monkeypatch, decision, when_local=None):
    """One attempt at the terminal with a scripted decision, optionally at a chosen local time."""
    monkeypatch.setattr(V, "verify", lambda frames, faces, eyes, settings: decision)
    if when_local:
        monkeypatch.setattr(entries, "utc_now", lambda: at(when_local))
    client = app.test_client()
    client.get("/terminal/")
    with client.session_transaction() as s:
        token = s["_csrf"]
    return client.post("/terminal/verify", content_type="multipart/form-data", headers={"X-CSRF-Token": token},
                       data={"frames": [(io.BytesIO(b"x"), f"f{i}.jpg") for i in range(12)]}).get_json()


def granted(student_id=1, **kw):
    return V.Decision(True, "granted", "Access granted.", method="Face", student_id=student_id, distance=0.2,
                      liveness="passed", **kw)


def test_first_granted_entry_creates_attendance_and_says_so(app, users, monkeypatch):
    body = gate(app, monkeypatch, granted(1), "2026-10-11 19:00")
    assert body["granted"] and body["attendance"] == {"status": "Present", "first_entry": True, "date": "2026-10-11"}
    assert len(records(app, 1)) == 1


def test_late_entry_is_marked_late_at_the_terminal(app, users, monkeypatch):
    body = gate(app, monkeypatch, granted(1), "2026-10-11 22:40")
    assert body["attendance"]["status"] == "Late"


def test_second_entry_is_logged_but_not_counted_again(app, users, monkeypatch):
    gate(app, monkeypatch, granted(1), "2026-10-11 18:00")
    body = gate(app, monkeypatch, granted(1), "2026-10-11 21:00")
    assert body["attendance"]["first_entry"] is False
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM entry_log WHERE result = 'Granted'").fetchone()[0] == 2
    assert len(records(app, 1)) == 1


@pytest.mark.parametrize("code,liveness", [("liveness_failed", "failed"), ("not_recognized", "passed"), ("no_face", "not_checked")])
def test_failed_attempts_never_create_attendance(app, users, monkeypatch, code, liveness):
    body = gate(app, monkeypatch, V.deny(code, liveness=liveness, distance=0.7), "2026-10-11 19:00")
    assert body["granted"] is False and body["attendance"] is None
    assert records(app) == []


def test_eye_region_entry_counts_as_an_entry_and_is_flagged(app, users, monkeypatch):
    d = V.Decision(True, "granted", "ok", method="Eye-Region", student_id=2, distance=0.55, liveness="passed", needs_review=True)
    body = gate(app, monkeypatch, d, "2026-10-11 19:00")
    assert body["attendance"]["status"] == "Present" and len(records(app, 2)) == 1
    with app.app_context():
        assert get_db().execute("SELECT needs_review FROM entry_log").fetchone()[0] == 1


def test_guest_entries_do_not_create_student_attendance(app, users):
    """Guest verification arrives in a later phase. A guest row in the log must never turn into attendance."""
    with app.app_context():
        db = get_db()
        db.execute("INSERT INTO student (registration_no,full_name,email,phone,room_no,department,session,gender,parent_name,"
                   "parent_email,password_hash) VALUES ('REG-9','Host','h@x.com','1','1','CS','2022','F','P','p@x.com','h')")
        db.execute("INSERT INTO guest_profile (full_name, contact, host_student_id, created_at) VALUES ('Visitor','1',3,'2026-10-11T10:00:00')")
        db.execute("INSERT INTO entry_log (subject_type, guest_id, attempted_at, method, result, door_status) "
                   "VALUES ('guest', 1, '2026-10-11T14:00:00', 'Face', 'Granted', 'Unlocked')")
        db.commit()
        assert db.execute("SELECT COUNT(*) FROM attendance").fetchone()[0] == 0


def test_log_row_and_attendance_are_saved_together_or_not_at_all(app, users, monkeypatch):
    def boom(*a, **k):
        raise sqlite3.OperationalError("disk full")
    monkeypatch.setattr(attendance, "mark_entry", boom)
    monkeypatch.setattr(V, "verify", lambda frames, faces, eyes, settings: granted(1))
    client = app.test_client()
    client.get("/terminal/")
    with client.session_transaction() as s:
        token = s["_csrf"]
    resp = client.post("/terminal/verify", content_type="multipart/form-data", headers={"X-CSRF-Token": token},
                       data={"frames": [(io.BytesIO(b"x"), f"f{i}.jpg") for i in range(12)]})
    body = resp.get_json()
    assert resp.status_code == 500 and body["ok"] is False and not body.get("granted")      # fails closed: no "Access granted"
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM entry_log").fetchone()[0] == 0        # rolled back with the attendance
    assert records(app) == []


def test_a_denied_decision_is_never_attributed_or_counted_even_if_it_carries_a_student(app, users):
    """Defence in depth: whatever the verification code returns, a denial must not name a student or mark attendance."""
    tricky = V.Decision(False, "liveness_failed", "x", method="Face", student_id=1, distance=0.1, liveness="failed")
    with app.app_context():
        entry_id, info = entries.record_attempt(get_db(), tricky, app.config)
        row = get_db().execute("SELECT * FROM entry_log WHERE entry_id = ?", (entry_id,)).fetchone()
    assert info is None and records(app) == []
    assert row["student_id"] is None and row["subject_type"] is None and row["method"] == "None" and row["result"] == "Denied"
