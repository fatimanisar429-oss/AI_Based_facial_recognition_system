"""Attendance, entry log, exit and settings pages: who may open them, what they show, filters, safety."""
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.db import get_db
from app.timeutil import hostel_today
from conftest import login, token_when_logged_in

KARACHI = ZoneInfo("Asia/Karachi")


def utc(local_text):
    """'2026-10-11 21:59' (Karachi) -> UTC text as stored in the database."""
    moment = datetime.strptime(local_text, "%Y-%m-%d %H:%M").replace(tzinfo=KARACHI).astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


@pytest.fixture
def data(app, users):
    """Attendance for two students over three days, plus entry attempts of every kind."""
    with app.app_context():
        db = get_db()
        for sid, day, tm, status in ((1, "2026-10-10", "18:00:00", "Present"), (1, "2026-10-11", "22:30:00", "Late"),
                                     (2, "2026-10-11", "19:00:00", "Present"), (2, "2026-10-12", "20:00:00", "Present")):
            db.execute("INSERT INTO attendance (student_id, local_date, local_time, status) VALUES (?,?,?,?)", (sid, day, tm, status))
        rows = [
            ("student", 1, "2026-10-11 18:00", "Face", "Granted", 0.21, None, 0, None, "passed"),
            ("student", 2, "2026-10-11 19:00", "Eye-Region", "Granted", 0.55, None, 1, None, "passed"),
            (None, None, "2026-10-11 20:00", "None", "Denied", None, "Liveness check failed (no blink detected)", 0, "liveness_failed", "failed"),
            (None, None, "2026-10-11 20:05", "None", "Denied", 0.71, "Face not recognised", 0, "not_recognized", "passed"),
            ("student", 1, "2026-10-12 03:59", "Face", "Granted", 0.30, None, 0, None, "passed"),    # still hostel day 11 Oct
            ("student", 1, "2026-10-12 04:00", "Face", "Granted", 0.30, None, 0, None, "passed"),    # hostel day 12 Oct
        ]
        for st, sid, local, method, result, score, reason, review, code, live in rows:
            db.execute("INSERT INTO entry_log (subject_type, student_id, attempted_at, method, result, score, failure_reason, door_status,"
                       " needs_review, failure_code, liveness) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (st, sid, utc(local), method, result, score, reason, "Unlocked" if result == "Granted" else "Locked", review, code, live))
        db.commit()


@pytest.fixture
def admin(client, users):
    login(client, "admin@x.com")
    return client


def as_warden(app):
    c = app.test_client()
    login(c, "warden@x.com")
    return c


def as_student(app, email="s1@x.com"):
    c = app.test_client()
    login(c, email)
    return c


# ================= who may open what =================

STAFF_PAGES = ["/attendance/", "/logs/", "/exits/"]


@pytest.mark.parametrize("path", STAFF_PAGES)
def test_staff_can_open_the_staff_pages(app, admin, path):
    assert admin.get(path).status_code == 200 and as_warden(app).get(path).status_code == 200


@pytest.mark.parametrize("path", STAFF_PAGES + ["/settings/"])
def test_students_cannot_open_staff_pages(app, users, path):
    assert as_student(app).get(path).status_code == 403


@pytest.mark.parametrize("path", STAFF_PAGES + ["/settings/", "/attendance/my"])
def test_anonymous_visitors_are_sent_to_login(client, users, path):
    resp = client.get(path)
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]


def test_settings_are_admin_only(app, users):
    assert as_warden(app).get("/settings/").status_code == 403


def test_my_attendance_is_for_students_only(app, admin):
    assert admin.get("/attendance/my").status_code == 403
    assert as_warden(app).get("/attendance/my").status_code == 403


# ================= staff attendance page =================

def test_default_view_is_todays_hostel_day(app, admin, data):
    today = hostel_today(app.config["TIMEZONE"], app.config["ATTENDANCE_DAY_START"])
    html = admin.get("/attendance/").get_data(as_text=True)
    assert f'value="{today}"' in html and "attendance-records" not in html


def test_date_range_filter(admin, data):
    html = admin.get("/attendance/?from=2026-10-11&to=2026-10-11").get_data(as_text=True)
    assert "Student 1" in html and "Student 2" in html and "2026-10-10" not in html and "2026-10-12" not in html
    wide = admin.get("/attendance/?from=2026-10-10&to=2026-10-12").get_data(as_text=True)
    assert wide.count("<tr>") == 5                                                       # header + 4 records


def test_status_filter_and_counts(admin, data):
    html = admin.get("/attendance/?from=2026-10-10&to=2026-10-12&status=Late").get_data(as_text=True)
    assert html.count("<tr>") == 2 and "22:30" in html
    summary = admin.get("/attendance/?from=2026-10-10&to=2026-10-12").get_data(as_text=True)
    assert 'count-num">3</span><span class="count-label">Present' in summary
    assert 'count-num">1</span><span class="count-label">Late' in summary


def test_student_search(admin, data):
    html = admin.get("/attendance/?from=2026-10-10&to=2026-10-12&q=student+2").get_data(as_text=True)
    assert "Student 2" in html and "Student 1" not in html
    assert b"No attendance records found" in admin.get("/attendance/?from=2026-10-10&to=2026-10-12&q=zzz").data


def test_reversed_bad_and_huge_ranges_are_handled(admin, data):
    swapped = admin.get("/attendance/?from=2026-10-12&to=2026-10-10").get_data(as_text=True)
    assert swapped.count("<tr>") == 5 and 'value="2026-10-10"' in swapped
    assert admin.get("/attendance/?from=garbage&to=also-garbage").status_code == 200
    assert admin.get("/attendance/?from=1900-01-01&to=2026-10-12").status_code == 200


def test_unknown_status_is_ignored(admin, data):
    assert admin.get("/attendance/?status=Bogus&from=2026-10-10&to=2026-10-12").get_data(as_text=True).count("<tr>") == 5


def test_pagination(app, admin, data):
    with app.app_context():
        db = get_db()
        for n in range(30):
            db.execute("INSERT INTO attendance (student_id, local_date, local_time, status) VALUES (1,?,'10:00:00','Present')", (f"2025-01-{n + 1:02d}",))
        db.commit()
    html = admin.get("/attendance/?from=2025-01-01&to=2025-01-30").get_data(as_text=True)
    assert "Page 1 of 3" in html and 'class="person"' in html
    assert "Page 3 of 3" in admin.get("/attendance/?from=2025-01-01&to=2025-01-30&page=99").get_data(as_text=True)


def test_search_text_cannot_inject_sql_or_script(app, admin, data):
    for q in ("' OR '1'='1", "x'; DROP TABLE attendance; --", "<script>alert(1)</script>"):
        resp = admin.get("/attendance/", query_string={"q": q, "from": "2026-10-10", "to": "2026-10-12"})
        assert resp.status_code == 200 and b"<script>alert(1)</script>" not in resp.data
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM attendance").fetchone()[0] == 4


def test_empty_state_before_anyone_has_entered(admin):
    assert b"No attendance records found" in admin.get("/attendance/").data


# ================= student: my attendance =================

def test_student_sees_only_their_own_records(app, data):
    html = as_student(app, "s1@x.com").get("/attendance/my?month=2026-10").get_data(as_text=True)
    assert "2026-10-10" in html and "2026-10-11" in html and "2026-10-12" not in html        # student 2's record is on the 12th
    assert "Student 2" not in html and "19:00" not in html                                    # student 2's 11 Oct time
    assert 'count-num">1</span><span class="count-label">Present' in html and 'count-num">1</span><span class="count-label">Late' in html


def test_other_students_data_cannot_be_requested(app, data):
    html = as_student(app, "s1@x.com").get("/attendance/my?month=2026-10&student_id=2&id=2").get_data(as_text=True)
    assert "2026-10-12" not in html and "Student 2" not in html


def test_each_student_gets_a_different_page(app, data):
    one = as_student(app, "s1@x.com").get("/attendance/my?month=2026-10").get_data(as_text=True)
    two = as_student(app, "s2@x.com").get("/attendance/my?month=2026-10").get_data(as_text=True)
    assert "2026-10-10" in one and "2026-10-10" not in two and "2026-10-12" in two


def test_my_entry_history_lists_only_my_granted_entries(app, data):
    html = as_student(app, "s2@x.com").get("/attendance/my").get_data(as_text=True)
    assert "Eye-Region" in html and "Face not recognised" not in html and "Liveness" not in html


def test_month_filter_and_bad_month(app, data):
    c = as_student(app, "s1@x.com")
    assert b"No records found" in c.get("/attendance/my?month=2025-01").data
    assert c.get("/attendance/my?month=banana").status_code == 200
    assert c.get("/attendance/my?month=2026-13").status_code == 200


def test_student_empty_states(app, users):
    html = as_student(app).get("/attendance/my").get_data(as_text=True)
    assert "No records found" in html and "No entries yet" in html


# ================= entry logs =================

LOG_RANGE = "from=2026-10-11&to=2026-10-12"


def test_log_shows_every_attempt_and_names_nobody_for_denied(admin, data):
    html = admin.get(f"/logs/?{LOG_RANGE}").get_data(as_text=True)
    assert html.count("Unidentified") == 2 and "Student 1" in html and "Student 2" in html
    assert "Liveness check failed" in html and "Face not recognised" in html and "0.710" in html
    assert 'count-num">4</span><span class="count-label">Granted' in html and 'count-num">2</span><span class="count-label">Denied' in html


def test_log_filters(admin, data):
    assert admin.get(f"/logs/?{LOG_RANGE}&result=Denied").get_data(as_text=True).count("<tr>") == 3
    assert "Student 2" not in admin.get(f"/logs/?{LOG_RANGE}&method=Face").get_data(as_text=True)
    review = admin.get(f"/logs/?{LOG_RANGE}&review=1").get_data(as_text=True)
    assert "Student 2" in review and "Student 1" not in review and "Review" in review
    assert "Student 1" in admin.get(f"/logs/?{LOG_RANGE}&q=student+1").get_data(as_text=True)
    assert b"No entry attempts found" in admin.get(f"/logs/?{LOG_RANGE}&q=nobody").data


def test_log_days_follow_the_hostel_day_boundary(admin, data):
    day11 = admin.get("/logs/?from=2026-10-11&to=2026-10-11").get_data(as_text=True)
    day12 = admin.get("/logs/?from=2026-10-12&to=2026-10-12").get_data(as_text=True)
    assert day11.count("<tr>") == 6 and day12.count("<tr>") == 2            # 03:59 on the 12th is still the 11th; 04:00 is the 12th


def test_log_times_are_shown_in_local_time(admin, data):
    assert "11 Oct 2026, 06:00:00 PM" in admin.get(f"/logs/?{LOG_RANGE}").get_data(as_text=True)


def test_log_is_read_only(admin, data):
    token = {"X-CSRF-Token": token_when_logged_in(admin)}                  # with a valid token, so only the route can refuse
    for method in ("post", "put", "delete", "patch"):
        assert getattr(admin, method)("/logs/", headers=token).status_code == 405
    assert admin.post("/logs/1/delete", headers=token).status_code == 404


def test_database_refuses_to_change_or_remove_log_rows(app, data):
    with app.app_context():
        db = get_db()
        with pytest.raises(sqlite3.IntegrityError, match="cannot be changed"):
            db.execute("UPDATE entry_log SET result = 'Granted' WHERE entry_id = 3")
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            db.execute("DELETE FROM entry_log WHERE entry_id = 3")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM entry_log")
        assert db.execute("SELECT COUNT(*) FROM entry_log").fetchone()[0] == 6
        assert db.execute("SELECT result FROM entry_log WHERE entry_id = 3").fetchone()[0] == "Denied"


def test_log_empty_state(admin):
    assert b"No entry attempts found" in admin.get("/logs/").data


def test_log_search_is_safe(admin, data):
    resp = admin.get("/logs/", query_string={"q": "'; DROP TABLE entry_log; --", "from": "2026-10-11", "to": "2026-10-12"})
    assert resp.status_code == 200 and b"No entry attempts found" in resp.data


# ================= exit records =================

def post_exit(client, student_id, token=None):
    return client.post("/exits/record", data={"student_id": student_id, "csrf_token": token or token_when_logged_in(client)})


def test_warden_records_an_exit(app, users):
    c = as_warden(app)
    resp = post_exit(c, 1)
    assert resp.status_code == 302
    with app.app_context():
        row = get_db().execute("SELECT * FROM exit_record").fetchone()
    assert (row["subject_type"], row["subject_id"], row["recorded_by_role"], row["recorded_by_id"]) == ("student", 1, "warden", 1)
    html = c.get("/exits/").get_data(as_text=True)
    assert "Student 1" in html and "Wendy Warden" in html and "(warden)" in html


def test_admin_can_record_too_and_name_is_shown(app, admin):
    post_exit(admin, 2)
    assert "Ada Admin" in admin.get("/exits/").get_data(as_text=True)


def test_students_cannot_record_exits(app, users):
    c = as_student(app)
    assert post_exit(c, 1).status_code == 403
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM exit_record").fetchone()[0] == 0


def test_exit_needs_a_csrf_token(app, admin):
    assert admin.post("/exits/record", data={"student_id": 1}).status_code == 400


def test_pressing_twice_records_one_exit(app, admin):
    post_exit(admin, 1)
    resp = post_exit(admin, 1)
    assert b"already recorded" in admin.get(resp.headers["Location"]).data
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM exit_record").fetchone()[0] == 1


def test_different_students_can_exit_together(app, admin):
    post_exit(admin, 1)
    post_exit(admin, 2)
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM exit_record").fetchone()[0] == 2


@pytest.mark.parametrize("bad", ["", "abc", "999", "-1"])
def test_bad_student_ids_are_refused(app, admin, bad):
    post_exit(admin, bad)
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM exit_record").fetchone()[0] == 0


def test_deactivated_students_cannot_be_recorded(app, admin):
    with app.app_context():
        get_db().execute("UPDATE student SET is_active = 0 WHERE student_id = 1")
        get_db().commit()
    post_exit(admin, 1)
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM exit_record").fetchone()[0] == 0
    assert "Student 1" not in admin.get("/exits/").get_data(as_text=True).split("<table")[0]      # not offered in the dropdown


def test_exit_changes_neither_attendance_nor_the_entry_log(app, admin, data):
    with app.app_context():
        before = [get_db().execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("attendance", "entry_log")]
    post_exit(admin, 1)
    with app.app_context():
        after = [get_db().execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("attendance", "entry_log")]
    assert before == after


def test_exit_list_filters_and_empty_state(app, admin):
    assert b"No exits recorded" in admin.get("/exits/").data
    post_exit(admin, 1)
    today = hostel_today(app.config["TIMEZONE"], app.config["ATTENDANCE_DAY_START"])
    assert "Student 1" in admin.get(f"/exits/?from={today}&to={today}").get_data(as_text=True)
    assert b"No exits recorded" in admin.get("/exits/?from=2020-01-01&to=2020-01-02").data
    assert b"No exits recorded" in admin.get(f"/exits/?from={today}&to={today}&q=zzz").data


def test_the_page_does_not_claim_exits_are_automatic(admin):
    html = admin.get("/exits/").get_data(as_text=True)
    assert "does not detect exits by itself" in html


# ================= settings =================

def test_admin_saves_the_curfew(app, admin):
    resp = admin.post("/settings/", data={"curfew_time": "21:30", "csrf_token": token_when_logged_in(admin)})
    assert resp.status_code == 302
    with app.app_context():
        assert get_db().execute("SELECT value FROM setting WHERE key='curfew_time'").fetchone()[0] == "21:30"
    assert 'value="21:30"' in admin.get("/settings/").get_data(as_text=True)


@pytest.mark.parametrize("bad,message", [
    ("", "HH:MM"), ("9pm", "HH:MM"), ("25:00", "HH:MM"), ("22:60", "HH:MM"), ("22.30", "HH:MM"),
    ("03:00", "later than the start"), ("04:00", "later than the start"),
])
def test_invalid_curfews_are_rejected_with_a_message(app, admin, bad, message):
    resp = admin.post("/settings/", data={"curfew_time": bad, "csrf_token": token_when_logged_in(admin)})
    assert resp.status_code == 400 and message in resp.get_data(as_text=True) and "is-invalid" in resp.get_data(as_text=True)
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM setting").fetchone()[0] == 0


def test_curfew_change_needs_csrf_and_the_admin_role(app, admin):
    assert admin.post("/settings/", data={"curfew_time": "21:00"}).status_code == 400
    w = as_warden(app)
    assert w.post("/settings/", data={"curfew_time": "21:00", "csrf_token": token_when_logged_in(w)}).status_code == 403
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM setting").fetchone()[0] == 0


def test_changing_the_curfew_affects_new_entries_only(app, admin, data):
    from app.services import attendance
    admin.post("/settings/", data={"curfew_time": "18:30", "csrf_token": token_when_logged_in(admin)})
    with app.app_context():
        db = get_db()
        info = attendance.mark_entry(db, 1, datetime(2026, 10, 14, 14, 0, tzinfo=timezone.utc), app.config)    # 19:00 Karachi
        db.commit()
        assert info["status"] == "Late"                                                                        # after the new 18:30 curfew
        old = db.execute("SELECT status FROM attendance WHERE student_id = 1 AND local_date = '2026-10-10'").fetchone()[0]
    assert old == "Present"                                                                                    # history untouched


def test_settings_page_shows_the_fixed_values(admin):
    html = admin.get("/settings/").get_data(as_text=True)
    assert "Asia/Karachi" in html and "04:00" in html and "Eye-region fallback" in html and ">Off<" in html


def test_student_dashboard_shows_the_saved_curfew(app, admin):
    admin.post("/settings/", data={"curfew_time": "21:15", "csrf_token": token_when_logged_in(admin)})
    assert "21:15" in as_student(app).get("/student/").get_data(as_text=True)


# ================= navigation =================

def test_sidebar_links_are_live(app, admin):
    html = admin.get("/admin/").get_data(as_text=True)
    for path in ("/attendance/", "/logs/", "/exits/", "/settings/"):
        assert f'href="{path}"' in html
    assert 'href="/attendance/my"' in as_student(app).get("/student/").get_data(as_text=True)
    warden = as_warden(app).get("/warden/").get_data(as_text=True)
    assert 'href="/settings/"' not in warden and 'href="/logs/"' in warden


def test_sidebar_highlights_the_current_section(admin):
    assert 'href="/logs/" class="nav-link-item active"' in admin.get("/logs/").get_data(as_text=True)
    assert 'href="/exits/" class="nav-link-item active"' in admin.get("/exits/").get_data(as_text=True)


def test_print_button_and_print_styles_exist(admin):
    assert "window.print()" in admin.get("/attendance/").get_data(as_text=True)
    assert "@media print" in admin.get("/static/css/app.css").get_data(as_text=True)
