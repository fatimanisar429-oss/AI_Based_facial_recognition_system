"""Daily, weekly and monthly reports: the calculations, the pages, and the registration-date rule."""
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import create_app
from app.db import get_db
from app.services import reports
from app.timeutil import hostel_today, to_text
from conftest import login

KARACHI = ZoneInfo("Asia/Karachi")


def utc(local_text):
    return datetime.strptime(local_text, "%Y-%m-%d %H:%M").replace(tzinfo=KARACHI).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# ================= choosing the period =================

@pytest.mark.parametrize("day,monday,sunday", [
    ("2026-10-14", "2026-10-12", "2026-10-18"),      # a Wednesday
    ("2026-10-12", "2026-10-12", "2026-10-18"),      # a Monday
    ("2026-10-18", "2026-10-12", "2026-10-18"),      # a Sunday belongs to the week that ends on it
    ("2026-12-31", "2026-12-28", "2027-01-03"),      # across New Year
    ("2026-03-01", "2026-02-23", "2026-03-01"),
])
def test_calendar_week_is_monday_to_sunday(day, monday, sunday):
    assert reports.calendar_week(day) == (monday, sunday)


@pytest.mark.parametrize("month,first,last", [
    ("2026-10", "2026-10-01", "2026-10-31"), ("2026-02", "2026-02-01", "2026-02-28"), ("2028-02", "2028-02-01", "2028-02-29"),
    ("2026-12", "2026-12-01", "2026-12-31"), ("2026-04", "2026-04-01", "2026-04-30"),
])
def test_calendar_month(month, first, last):
    assert reports.calendar_month(month) == (first, last)


def test_period_for_each_kind_and_its_title():
    assert reports.period_for("daily", "2026-10-14", None, None, "2026-10-20") == ("2026-10-14", "2026-10-14", "Daily report: Wednesday, 14 October 2026")
    assert reports.period_for("daily", None, None, None, "2026-10-20")[:2] == ("2026-10-20", "2026-10-20")
    assert reports.period_for("weekly", None, "2026-10-14", None, "2026-10-20") == ("2026-10-12", "2026-10-18", "Weekly report: 12 Oct to 18 Oct 2026")
    assert reports.period_for("weekly", None, None, None, "2026-10-20")[:2] == ("2026-10-19", "2026-10-25")
    assert reports.period_for("monthly", None, None, "2026-09", "2026-10-20") == ("2026-09-01", "2026-09-30", "Monthly report: September 2026")
    assert reports.period_for("monthly", None, None, None, "2026-10-20")[:2] == ("2026-10-01", "2026-10-31")


# ================= per-student statistics and the registration rule =================

def add_student(app, n, created_at=None, active=1, name=None):
    with app.app_context():
        db = get_db()
        sid = db.execute("INSERT INTO student (registration_no,full_name,email,phone,room_no,department,session,gender,parent_name,parent_email,password_hash,is_active,created_at) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (f"R-{n:03d}", name or f"Report {n}", f"r{n}@x.com", "1", f"R{n}", "CS", "2022", "F", "P", "p@x.com", "h", active, created_at)).lastrowid
        db.commit()
        return sid


def attend(app, sid, days, status="Present", time="20:00:00"):
    with app.app_context():
        for d in days:
            get_db().execute("INSERT INTO attendance (student_id, local_date, local_time, status) VALUES (?,?,?,?)", (sid, d, time, status))
        get_db().commit()


def stat(app, sid, first, last, registered=None):
    with app.app_context():
        return reports.student_period_stats(get_db(), sid, first, last, registered)


def test_a_student_registered_mid_period_is_not_absent_before_registration(app, users):
    attend(app, 1, ["2026-10-14", "2026-10-15"])
    full = stat(app, 1, "2026-10-12", "2026-10-18")
    cut = stat(app, 1, "2026-10-12", "2026-10-18", registered="2026-10-14")
    assert (full["days"], full["days_absent"], full["percentage"]) == (7, 5, 28.6)
    assert (cut["days"], cut["days_present"], cut["days_absent"], cut["percentage"]) == (5, 2, 3, 40.0)


def test_registered_after_the_period_has_no_days(app, users):
    s = stat(app, 1, "2026-10-12", "2026-10-18", registered="2026-10-25")
    assert s["days"] == 0 and s["percentage"] is None and s["days_absent"] == 0


def test_a_registration_before_the_period_changes_nothing(app, users):
    assert stat(app, 1, "2026-10-12", "2026-10-18", registered="2026-01-01") == stat(app, 1, "2026-10-12", "2026-10-18")


# ================= the attendance report =================

def report(app, first, last, today, kind="weekly", q="", order="name"):
    with app.app_context():
        return reports.attendance_report(get_db(), app.config, first, last, today, kind, q, order)


def by_name(result):
    return {r["full_name"]: r for r in result["rows"]}


def test_weekly_attendance_report_numbers(app, users):
    attend(app, 1, ["2026-10-12", "2026-10-13", "2026-10-14"])
    attend(app, 1, ["2026-10-15"], "Late", "23:10:00")
    attend(app, 2, ["2026-10-12"])
    r = report(app, "2026-10-12", "2026-10-18", today="2026-10-25")
    one, two = by_name(r)["Student 1"], by_name(r)["Student 2"]
    assert (one["days"], one["days_present"], one["late_entries"], one["days_absent"], one["percentage"]) == (7, 4, 1, 3, 57.1)
    assert (two["days_present"], two["days_absent"], two["percentage"]) == (1, 6, 14.3)
    s = r["summary"]
    assert (s["students"], s["days_present"], s["late"], s["absent"], s["perfect"], s["average"]) == (2, 5, 1, 9, 0, 35.7)
    assert not r["partial"] and not r["future"] and r["elapsed_days"] == 7


def test_only_days_that_have_happened_count(app, users):
    attend(app, 1, ["2026-10-12"])
    r = report(app, "2026-10-12", "2026-10-18", today="2026-10-14")                   # Wednesday: Mon-Wed have happened
    one = by_name(r)["Student 1"]
    assert (one["days"], one["days_present"], one["days_absent"], one["percentage"]) == (3, 1, 2, 33.3)
    assert r["partial"] and r["elapsed_days"] == 3 and r["through"] == "2026-10-14"


def test_a_period_that_has_not_started_reports_nobody_absent(app, users):
    r = report(app, "2026-11-02", "2026-11-08", today="2026-10-14")
    assert r["future"] and r["elapsed_days"] == 0
    assert all(row["days"] == 0 and row["days_absent"] == 0 and row["percentage"] is None for row in r["rows"])
    assert r["summary"]["average"] is None


def test_students_are_counted_only_from_their_registration_day(app, users):
    add_student(app, 3, created_at=utc("2026-10-15 09:00"), name="Newcomer")
    attend(app, 3, ["2026-10-16"])
    r = report(app, "2026-10-12", "2026-10-18", today="2026-10-25")
    n = by_name(r)["Newcomer"]
    assert (n["days"], n["days_present"], n["days_absent"], n["percentage"]) == (4, 1, 3, 25.0)     # 15th to 18th
    assert by_name(r)["Student 1"]["days"] == 7                                                    # accounts without a date count the full period


def test_registration_after_midnight_belongs_to_the_previous_hostel_day(app, users):
    add_student(app, 3, created_at=utc("2026-10-15 00:30"), name="Night Owl")                      # 00:30 on the 15th is still the 14th
    assert by_name(report(app, "2026-10-12", "2026-10-18", "2026-10-25"))["Night Owl"]["days"] == 5


def test_students_registered_after_the_period_are_left_out(app, users):
    add_student(app, 3, created_at=utc("2026-11-01 09:00"), name="Too New")
    assert "Too New" not in by_name(report(app, "2026-10-12", "2026-10-18", "2026-11-05"))


def test_deactivated_students_are_not_in_the_report(app, users):
    add_student(app, 3, active=0, name="Gone")
    assert "Gone" not in by_name(report(app, "2026-10-12", "2026-10-18", "2026-10-25"))


def test_perfect_attendance_is_counted(app, users):
    attend(app, 1, [f"2026-10-{d}" for d in range(12, 19)])
    r = report(app, "2026-10-12", "2026-10-18", "2026-10-25")
    assert by_name(r)["Student 1"]["percentage"] == 100.0 and r["summary"]["perfect"] == 1


def test_daily_report_shows_a_status_per_student(app, users):
    attend(app, 1, ["2026-10-14"])
    attend(app, 2, ["2026-10-14"], "Late", "22:45:00")
    add_student(app, 3, name="Missing")
    past = by_name(report(app, "2026-10-14", "2026-10-14", today="2026-10-20", kind="daily"))
    assert (past["Student 1"]["status"], past["Student 1"]["first_entry"]) == ("Present", "20:00")
    assert (past["Student 2"]["status"], past["Student 2"]["first_entry"]) == ("Late", "22:45")
    assert past["Missing"]["status"] == "Absent"
    now = by_name(report(app, "2026-10-14", "2026-10-14", today="2026-10-14", kind="daily"))
    assert now["Missing"]["status"] == "Not entered yet"                                           # the day is not over


def test_search_filters_the_rows(app, users):
    add_student(app, 3, name="Zainab Raza")
    r = report(app, "2026-10-12", "2026-10-18", "2026-10-25", q="zainab")
    assert list(by_name(r)) == ["Zainab Raza"]
    assert report(app, "2026-10-12", "2026-10-18", "2026-10-25", q="%")["rows"] == []              # % is not a wildcard
    assert report(app, "2026-10-12", "2026-10-18", "2026-10-25", q="R3")["rows"][0]["full_name"] == "Zainab Raza"


def test_sorting(app, users):
    add_student(app, 3, name="Aaa First")
    attend(app, 1, ["2026-10-12", "2026-10-13", "2026-10-14"])
    attend(app, 2, ["2026-10-12"], "Late")
    attend(app, 2, ["2026-10-13"], "Late")
    names = lambda order: [r["full_name"] for r in report(app, "2026-10-12", "2026-10-18", "2026-10-25", order=order)["rows"]]
    assert names("name") == ["Aaa First", "Student 1", "Student 2"]
    assert names("attendance") == ["Aaa First", "Student 2", "Student 1"]
    assert names("late")[0] == "Student 2"
    html = as_(app, "admin@x.com").get("/reports/?type=weekly&order=bogus").get_data(as_text=True)
    assert re.search(r'<option value="name"\s+selected', html)                                  # an unknown order falls back to name


def test_no_students_at_all(app):
    r = report(app, "2026-10-12", "2026-10-18", "2026-10-25")
    assert r["rows"] == [] and r["summary"]["students"] == 0 and r["summary"]["average"] is None


# ================= the security report =================

def log(app, local, result="Granted", subject="student", method="Face", code=None, review=0):
    with app.app_context():
        get_db().execute(
            "INSERT INTO entry_log (subject_type, attempted_at, method, result, door_status, failure_code, needs_review) VALUES (?,?,?,?,?,?,?)",
            (subject if result == "Granted" else None, utc(local), method if result == "Granted" else "None", result,
             "Unlocked" if result == "Granted" else "Locked", code, review))
        get_db().commit()


def security(app, first="2026-10-12", last="2026-10-18"):
    with app.app_context():
        return reports.security_report(get_db(), app.config, first, last)


def test_security_report_counts(app, users):
    log(app, "2026-10-12 18:00")
    log(app, "2026-10-12 19:00", method="Eye-Region", review=1)
    log(app, "2026-10-13 10:00", subject="guest")
    log(app, "2026-10-13 20:00", "Denied", code="liveness_failed")
    log(app, "2026-10-13 20:05", "Denied", code="not_recognized")
    log(app, "2026-10-13 20:06", "Denied", code="not_recognized")
    log(app, "2026-10-20 12:00")                                         # outside the period
    s = security(app)
    assert (s["attempts"], s["granted"], s["denied"], s["review"]) == (6, 3, 3, 1)
    assert (s["student_entries"], s["guest_entries"], s["face"], s["eye"]) == (2, 1, 2, 1)
    assert s["reasons"] == [("Face not recognised", 2), ("Liveness check failed (no blink detected)", 1)]


def test_security_report_groups_by_hostel_day(app, users):
    log(app, "2026-10-12 03:59")                     # still the 11th: outside this week
    log(app, "2026-10-12 04:00")                     # the 12th
    log(app, "2026-10-19 03:59")                     # still the 18th: inside
    log(app, "2026-10-19 04:00")                     # the 19th: outside
    s = security(app)
    assert s["attempts"] == 2
    by_label = {d["full"]: d["values"] for d in s["chart_days"]}
    assert by_label["Mon 12 Oct"] == [1, 0] and by_label["Sun 18 Oct"] == [1, 0] and len(s["chart_days"]) == 7


def test_security_report_alerts_exits_and_visits(app, users):
    with app.app_context():
        db = get_db()
        db.execute("INSERT INTO alert (student_id, alert_type, message, created_at, status) VALUES (1,'Emergency','e',?, 'Acknowledged')", (utc("2026-10-13 10:00"),))
        db.execute("INSERT INTO alert (student_id, alert_type, message, created_at, status) VALUES (2,'Emergency','e2',?, 'New')", (utc("2026-10-14 10:00"),))
        db.execute("INSERT INTO alert (alert_type, message, created_at, status) VALUES ('Unauthorized Attempt','u',?, 'New')", (utc("2026-10-15 10:00"),))
        db.execute("INSERT INTO exit_record (subject_type, subject_id, recorded_by_role, recorded_by_id, recorded_at) VALUES ('student',1,'warden',1,?)", (utc("2026-10-13 22:00"),))
        db.execute("INSERT INTO exit_record (subject_type, subject_id, recorded_by_role, recorded_by_id, recorded_at) VALUES ('student',2,'warden',1,?)", (utc("2026-10-13 22:05"),))
        db.execute("INSERT INTO guest_profile (full_name, contact, host_student_id, created_at) VALUES ('G','1',1,'2026-10-01T00:00:00')")
        for status in ("Approved", "Approved", "Rejected", "Pending"):
            db.execute("INSERT INTO guest_visit_request (student_id, guest_id, visit_date, valid_from, valid_to, purpose, status, created_at) VALUES (1,1,'2026-10-14','a','b','p',?, 'c')", (status,))
        db.commit()
    s = security(app)
    assert s["emergency"] == {"total": 2, "acknowledged": 1, "open": 1} and s["unauthorized"] == {"total": 1, "acknowledged": 0, "open": 1}
    assert s["alerts_total"] == 3 and s["alerts"][0]["alert_type"] == "Unauthorized Attempt"           # newest first
    assert s["exits"] == {"student": 2} and s["visits"] == {"total": 4, "approved": 2, "rejected": 1, "pending": 1}


def test_an_empty_period_is_all_zeros(app, users):
    s = security(app)
    assert (s["attempts"], s["alerts_total"], s["reasons"], s["exits"]) == (0, 0, [], {})
    assert all(d["values"] == [0, 0] for d in s["chart_days"])


def test_a_month_has_one_chart_bar_per_day(app, users):
    assert len(security(app, "2026-10-01", "2026-10-31")["chart_days"]) == 31


# ================= the saved parent reports =================

def parent_rows(app, **kw):
    with app.app_context():
        return reports.parent_reports(get_db(), kw.pop("first", "2026-10-01"), kw.pop("last", "2026-10-31"), **kw)


def test_parent_report_history(app, users):
    with app.app_context():
        for sid, end, status in ((1, "2026-10-11", "Sent"), (2, "2026-10-11", "Failed"), (1, "2026-10-18", "Pending"), (1, "2026-09-27", "Sent")):
            get_db().execute("INSERT INTO report (student_id, period_start, period_end, days_present, late_entries, attendance_percentage, email_status, generated_date) "
                             "VALUES (?,?,?,5,1,71.4,?,'2026-10-19')", (sid, (date.fromisoformat(end) - timedelta(days=6)).isoformat(), end, status))
        get_db().commit()
    assert parent_rows(app)["total"] == 3                                                           # the September one is outside
    assert parent_rows(app, status="Failed")["total"] == 1 and parent_rows(app, status="bogus")["total"] == 3
    assert parent_rows(app, q="student 2")["total"] == 1 and parent_rows(app, q="%")["total"] == 0
    assert parent_rows(app)["rows"][0]["period_end"] == "2026-10-18"


# ================= the weekly parent email and new students =================

def test_weekly_reports_skip_students_registered_after_the_week_and_prorate_new_ones(app, users):
    add_student(app, 3, created_at=utc("2026-10-15 09:00"), name="Mid Week")
    add_student(app, 4, created_at=utc("2026-10-20 09:00"), name="Next Week")
    attend(app, 3, ["2026-10-16"])
    with app.app_context():
        result = reports.generate_weekly(get_db(), app.config, "2026-10-19", "2026-10-19T15:00:00")
        rows = get_db().execute("SELECT s.full_name, r.days_present, r.attendance_percentage FROM report r JOIN student s ON s.student_id = r.student_id").fetchall()
    names = {r["full_name"]: (r["days_present"], r["attendance_percentage"]) for r in rows}
    assert "Next Week" not in names and names["Mid Week"] == (1, 25.0) and result["created"] == 3        # Mid Week counts 15th to 18th: 1 of 4 days
    assert result["existing"] == 0 and result["period"] == ("2026-10-12", "2026-10-18")      # the skipped student is not counted as "already there"


def test_previous_week_is_the_seven_complete_days_before_today():
    assert reports.previous_week("2026-10-19") == ("2026-10-12", "2026-10-18")
    assert reports.previous_week("2026-10-18") == ("2026-10-11", "2026-10-17")
    assert reports.previous_week("2026-01-03") == ("2025-12-27", "2026-01-02")


# ================= the pages =================

def as_(app, email):
    c = app.test_client()
    login(c, email)
    return c


def today_of(app):
    return hostel_today(app.config["TIMEZONE"], app.config["ATTENDANCE_DAY_START"])


@pytest.mark.parametrize("url", ["/reports/", "/reports/?type=daily", "/reports/?type=monthly", "/reports/?tab=security", "/reports/?tab=parents"])
def test_staff_can_open_every_report(app, users, url):
    assert as_(app, "admin@x.com").get(url).status_code == 200 and as_(app, "warden@x.com").get(url).status_code == 200


@pytest.mark.parametrize("url", ["/reports/", "/reports/?tab=security", "/reports/?tab=parents"])
def test_students_cannot_open_reports_and_visitors_are_sent_to_login(app, users, url):
    assert as_(app, "s1@x.com").get(url).status_code == 403
    resp = app.test_client().get(url)
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]


@pytest.mark.parametrize("query", ["type=zzz", "tab=zzz", "date=garbage&type=daily", "week=2026-99-99&type=weekly", "month=banana&type=monthly",
                                   "order=bogus", "page=-5&tab=parents", "status=%27%3B+DROP+TABLE+report%3B--&tab=parents", "q=%25%25"])
def test_bad_parameters_fall_back_instead_of_failing(app, users, query):
    assert as_(app, "admin@x.com").get(f"/reports/?{query}").status_code == 200


def test_default_view_is_the_weekly_attendance_report(app, users):
    html = as_(app, "admin@x.com").get("/reports/").get_data(as_text=True)
    assert "Weekly report:" in html and "Attendance</a>" in html and "Student 1" in html and "Average attendance" in html


def test_report_table_shows_each_students_numbers(app, users):
    today = today_of(app)
    attend(app, 1, [today])
    html = as_(app, "warden@x.com").get(f"/reports/?type=daily&date={today}").get_data(as_text=True)
    assert "Daily report:" in html and "Student 1" in html and "Present" in html and "Not entered yet" in html
    monthly = as_(app, "warden@x.com").get("/reports/?type=monthly").get_data(as_text=True)
    assert "Monthly report:" in monthly and 'class="pct-bar"' in monthly and "Days absent" in monthly


def test_a_future_period_says_so(app, users):
    html = as_(app, "admin@x.com").get("/reports/?type=monthly&month=2099-01").get_data(as_text=True)
    assert "has not started" in html and "Student 1" not in html


def test_a_finished_period_has_no_unfinished_notice(app, users):
    html = as_(app, "admin@x.com").get("/reports/?type=weekly&week=2020-01-06").get_data(as_text=True)
    assert "Weekly report: 06 Jan to 12 Jan 2020" in html and "not over yet" not in html and "Student 1" in html


def test_search_and_sort_on_the_page(app, users):
    c = as_(app, "admin@x.com")
    only = c.get("/reports/?type=monthly&q=student+2").get_data(as_text=True)
    assert "Student 2" in only and "Student 1" not in only
    assert "No students to report on" in c.get("/reports/?type=monthly&q=zzz").get_data(as_text=True)
    assert 'value="lowest attendance"' not in only and "Sort: lowest attendance" in only


def test_print_layout_is_present(app, users):
    html = as_(app, "admin@x.com").get("/reports/?type=daily").get_data(as_text=True)
    assert 'class="print-header"' in html and "Daily report:" in html.split('class="print-header"')[1] and "Generated" in html and "Ada Admin" in html
    assert "window.print()" in html and html.count("no-print") >= 3
    css = as_(app, "admin@x.com").get("/static/css/app.css").get_data(as_text=True)
    assert "@media print" in css and ".no-print { display: none" in css and ".print-header { display: block" in css


def test_tabs_keep_the_chosen_period(app, users):
    html = as_(app, "admin@x.com").get("/reports/?type=monthly&month=2026-09&q=abc").get_data(as_text=True)
    assert re.search(r'href="/reports/\?tab=security[^"]*month=2026-09', html) and "q=abc" in html.split("Security</a>")[0]


def test_security_tab_content(app, users):
    log(app, "2026-10-13 20:00", "Denied", code="not_recognized")
    log(app, "2026-10-13 18:00")
    html = as_(app, "admin@x.com").get("/reports/?tab=security&type=weekly&week=2026-10-14").get_data(as_text=True)
    assert "Weekly report: 12 Oct to 18 Oct 2026" in html and "Face not recognised" in html and "Entry attempts per day" in html
    assert 'count-num">2</span><span class="count-label">Entry attempts' in html and "Granted entries" in html
    assert "No activity in this period" in as_(app, "admin@x.com").get("/reports/?tab=security&type=weekly&week=2020-01-06").get_data(as_text=True)


def test_parent_tab_content_and_empty_state(app, users):
    c = as_(app, "admin@x.com")
    assert "No parent reports in this period" in c.get("/reports/?tab=parents").get_data(as_text=True)
    with app.app_context():
        get_db().execute("INSERT INTO report (student_id, period_start, period_end, days_present, late_entries, attendance_percentage, email_status, generated_date) "
                         "VALUES (1,'2026-10-05','2026-10-11',6,1,85.7,'Failed','2026-10-12')")
        get_db().commit()
    html = c.get("/reports/?tab=parents&type=monthly&month=2026-10").get_data(as_text=True)
    assert "Student 1" in html and "85.7%" in html and "Failed" in html and "p@x.com" in html
    assert "No parent reports" in c.get("/reports/?tab=parents&type=monthly&month=2026-10&status=Sent").get_data(as_text=True)


def test_report_pages_are_safe(app, users):
    c = as_(app, "admin@x.com")
    for q in ("<script>alert(1)</script>", "' OR 1=1 --", "x'; DROP TABLE attendance; --"):
        for tab in ("attendance", "parents"):
            resp = c.get("/reports/", query_string={"q": q, "tab": tab})
            assert resp.status_code == 200 and b"<script>alert(1)</script>" not in resp.data
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM student").fetchone()[0] == 2
    add_student(app, 3, name="<img src=x onerror=alert(1)>")
    assert b"<img src=x" not in c.get("/reports/?type=monthly").data


def test_dashboards_link_to_reports(app, users):
    assert 'href="/reports/"' in as_(app, "admin@x.com").get("/admin/").get_data(as_text=True)
    assert 'href="/reports/"' in as_(app, "warden@x.com").get("/warden/").get_data(as_text=True)
    assert "/reports/" not in as_(app, "s1@x.com").get("/student/").get_data(as_text=True)


# ================= registration date is recorded =================

def test_new_students_get_a_registration_time(app, users):
    from conftest import token_when_logged_in
    c = as_(app, "admin@x.com")
    data = {"full_name": "Fresh Student", "registration_no": "fresh-1", "email": "fresh@x.com", "phone": "0300 1234567", "gender": "Female", "room_no": "9",
            "session": "2022-2026", "department": "CS", "parent_name": "Parent Name", "parent_email": "p@x.com", "warden_id": "", "password": "longenough1"}
    resp = c.post("/students/new", data={**data, "csrf_token": token_when_logged_in(c)})
    assert resp.status_code == 302, re.findall(r"invalid-feedback[^>]*>([^<]+)", resp.get_data(as_text=True))
    with app.app_context():
        created = get_db().execute("SELECT created_at FROM student WHERE registration_no = 'FRESH-1'").fetchone()[0]
    assert abs((datetime.strptime(created, "%Y-%m-%dT%H:%M:%S") - datetime.utcnow()).total_seconds()) < 60


def test_old_databases_get_the_column(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE student (student_id INTEGER PRIMARY KEY AUTOINCREMENT, registration_no TEXT NOT NULL UNIQUE, full_name TEXT NOT NULL, email TEXT NOT NULL UNIQUE, "
                "phone TEXT NOT NULL, room_no TEXT NOT NULL, department TEXT NOT NULL, session TEXT NOT NULL, gender TEXT NOT NULL, parent_name TEXT NOT NULL, parent_email TEXT NOT NULL, "
                "password_hash TEXT NOT NULL, warden_id INTEGER, is_active INTEGER NOT NULL DEFAULT 1)")
    con.execute("INSERT INTO student (registration_no, full_name, email, phone, room_no, department, session, gender, parent_name, parent_email, password_hash) VALUES ('R','Old','o@x.com','1','1','CS','2022','F','P','p@x.com','h')")
    con.commit()
    con.close()
    app = create_app({"TESTING": True, "SECRET_KEY": "k", "DATABASE_PATH": str(path), "SCHEDULER_ENABLED": False})
    with app.app_context():
        db = get_db()
        assert "created_at" in {r[1] for r in db.execute("PRAGMA table_info(student)")}
        assert db.execute("SELECT created_at FROM student").fetchone()[0] is None and db.execute("SELECT COUNT(*) FROM student").fetchone()[0] == 1
