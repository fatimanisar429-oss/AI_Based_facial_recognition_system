import pytest

from app.db import get_db
from app.timeutil import hostel_today
from conftest import login


@pytest.fixture
def activity(app, users):
    """Entry and attendance data for student 1 and student 2, plus alerts."""
    today = hostel_today(app.config["TIMEZONE"], app.config["ATTENDANCE_DAY_START"])
    with app.app_context():
        db = get_db()
        # student 1: one granted entry (Present). student 2: one granted entry (Late) and one denied.
        db.execute("INSERT INTO entry_log (subject_type,student_id,attempted_at,method,result,door_status) "
                   "VALUES ('student',1,'2026-10-11T10:00:00','Face','Granted','Unlocked')")
        db.execute("INSERT INTO entry_log (subject_type,student_id,attempted_at,method,result,door_status) "
                   "VALUES ('student',2,'2026-10-11T11:00:00','Eye-Region','Granted','Unlocked')")
        db.execute("INSERT INTO entry_log (subject_type,student_id,attempted_at,method,result,failure_reason,door_status) "
                   "VALUES ('student',2,'2026-10-11T12:00:00','None','Denied','SECRET-REASON-S2','Locked')")
        db.execute("INSERT INTO entry_log (attempted_at,method,result,failure_reason,door_status) "
                   "VALUES ('2026-10-11T13:00:00','None','Denied','No match','Locked')")
        db.execute("INSERT INTO attendance (student_id,local_date,local_time,status) VALUES (1,?,'20:00','Present')", (today,))
        db.execute("INSERT INTO attendance (student_id,local_date,local_time,status) VALUES (2,?,'23:30','Late')", (today,))
        db.execute("INSERT INTO alert (student_id,alert_type,message,created_at,status) "
                   "VALUES (NULL,'Unauthorized Attempt','3 failed attempts in 5 minutes','2026-10-11T13:05:00','New')")
        db.execute("INSERT INTO alert (student_id,alert_type,message,created_at,status) "
                   "VALUES (2,'Emergency','Emergency button pressed','2026-10-11T14:00:00','New')")
        db.commit()


# ---------- empty states ----------

@pytest.mark.parametrize("email,path,text", [
    ("admin@x.com", "/admin/", b"No entries yet"),
    ("warden@x.com", "/warden/", b"No active security alerts"),
    ("s1@x.com", "/student/", b"No entry history yet"),
])
def test_empty_states_shown(client, users, email, path, text):
    login(client, email)
    resp = client.get(path)
    assert resp.status_code == 200 and text in resp.data


# ---------- admin ----------

def test_admin_dashboard_counts(client, activity):
    login(client, "admin@x.com")
    html = client.get("/admin/").get_data(as_text=True)
    assert "Registered students" in html and "Wardens" in html
    assert "Student 1" in html and "Student 2" in html
    assert "Unidentified" in html            # unknown person is never given a student's name
    assert "Eye-Region" in html


# ---------- warden ----------

def test_warden_dashboard_shows_alerts(client, activity):
    login(client, "warden@x.com")
    html = client.get("/warden/").get_data(as_text=True)
    assert "3 failed attempts in 5 minutes" in html
    assert "Emergency button pressed" in html and "Student 2" in html


# ---------- student privacy ----------

def test_student_sees_only_own_history(client, activity):
    login(client, "s1@x.com")
    html = client.get("/student/").get_data(as_text=True)
    assert "Face" in html
    assert "Eye-Region" not in html          # student 2's entry
    assert "SECRET-REASON-S2" not in html and "Student 2" not in html


def test_student_attendance_status_is_own(client, activity):
    # Student 2 was Late today (yellow badge); student 1 was Present (no yellow badge anywhere).
    login(client, "s2@x.com")
    html2 = client.get("/student/").get_data(as_text=True)
    assert "pill-warn" in html2 and "First entry at 23:30" in html2

    other = client.application.test_client()
    login(other, "s1@x.com")
    html1 = other.get("/student/").get_data(as_text=True)
    assert "pill-warn" not in html1 and "First entry at 20:00" in html1


def test_student_cannot_pick_another_id_via_query(client, activity):
    login(client, "s1@x.com")
    html = client.get("/student/?student_id=2").get_data(as_text=True)
    assert "Eye-Region" not in html and "Student 2" not in html


# ---------- layout ----------

def test_times_shown_in_local_timezone(client, activity):
    login(client, "admin@x.com")
    html = client.get("/admin/").get_data(as_text=True)
    assert "03:00 PM" in html                 # 10:00 UTC -> 15:00 in Asia/Karachi (UTC+5)


@pytest.mark.parametrize("email,path", [("admin@x.com", "/admin/"), ("warden@x.com", "/warden/"), ("s1@x.com", "/student/")])
def test_layout_has_nav_name_role_and_logout(client, users, email, path):
    login(client, email)
    html = client.get(path).get_data(as_text=True)
    assert 'name="viewport"' in html
    assert 'id="sidebar"' in html and "offcanvas" in html   # sidebar + mobile slide-in menu
    assert 'aria-label="Open menu"' in html                 # mobile menu button
    assert "Log out" in html and "csrf_token" in html


def test_static_assets_load(client):
    for path in ("/static/vendor/bootstrap.min.css", "/static/vendor/bootstrap.bundle.min.js",
                 "/static/css/app.css", "/static/js/app.js"):
        assert client.get(path).status_code == 200, path


def test_login_page_has_labels_and_password_toggle(client):
    html = client.get("/login").get_data(as_text=True)
    assert 'for="email"' in html and 'for="password"' in html
    assert "data-toggle-password" in html


def test_error_page_uses_layout(client):
    resp = client.get("/missing")
    assert resp.status_code == 404 and b"Page not found" in resp.data and b"bootstrap.min.css" in resp.data
