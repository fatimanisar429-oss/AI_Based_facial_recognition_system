import sqlite3

import pytest

from app.db import get_db, init_db


def test_health_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


def test_home_opens_the_entry_terminal_on_the_gate_computer(client):
    resp = client.get("/")                                       # the test client is 127.0.0.1, the gate computer
    assert resp.status_code == 302 and "/terminal/" in resp.headers["Location"]
    page = client.get("/terminal/").get_data(as_text=True)
    assert "Staff login" in page and 'href="/login"' in page     # the button that leads to the Admin / Warden login


def test_home_sends_other_computers_to_the_login_page(client):
    resp = client.get("/", environ_overrides={"REMOTE_ADDR": "192.168.1.50"})
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]
    assert client.get("/terminal/", environ_overrides={"REMOTE_ADDR": "192.168.1.50"}).status_code == 403


def test_all_tables_created(app):
    con = sqlite3.connect(app.config["DATABASE_PATH"])
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    expected = {
        "admin", "warden", "student", "guest_profile", "biometric_profile",
        "guest_visit_request", "entry_log", "attendance", "exit_record",
        "alert", "emergency_contact", "report", "setting", "login_attempt",
    }
    assert expected <= names


def test_foreign_keys_enforced(app):
    with app.app_context():
        db = get_db()
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO attendance (student_id, local_date, local_time, status) "
                "VALUES (999, '2026-01-01', '10:00', 'Present')"
            )


def test_duplicate_daily_attendance_blocked(app):
    with app.app_context():
        db = get_db()
        db.execute("INSERT INTO student (registration_no, full_name, email, phone, room_no, department,"
                   " session, gender, parent_name, parent_email, password_hash)"
                   " VALUES ('R1','A','a@x.com','1','1','CS','2022','F','P','p@x.com','h')")
        sql = "INSERT INTO attendance (student_id, local_date, local_time, status) VALUES (1,'2026-01-01','10:00','Present')"
        db.execute(sql)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(sql)


def test_init_db_keeps_existing_data(app):
    with app.app_context():
        db = get_db()
        db.execute("INSERT INTO setting (key, value) VALUES ('curfew', '22:00')")
        db.commit()
        init_db()  # run again
        assert db.execute("SELECT value FROM setting WHERE key='curfew'").fetchone()[0] == "22:00"
