"""Phase 5: student and warden management, validation, search, permissions."""
import re

import pytest
from werkzeug.security import check_password_hash

from app.db import get_db
from conftest import PASSWORD, login, token_when_logged_in

VALID_STUDENT = {
    "full_name": "Ayesha Khan", "registration_no": "bsse-001", "email": "Ayesha@Example.com",
    "phone": "+92 300 1234567", "gender": "Female", "room_no": "A-12", "session": "2022-2026",
    "department": "Software Engineering", "parent_name": "Mr Khan", "parent_email": "parent@example.com",
    "warden_id": "", "password": "longenough1",
}
VALID_WARDEN = {"full_name": "Sara Warden", "email": "sara@example.com", "phone": "03001234567",
                "gender": "Female", "password": "longenough1"}


def post(client, path, data):
    """POST with the session's CSRF token (read from any rendered page first)."""
    client.get("/students/")
    with client.session_transaction() as s:
        token = s["_csrf"]
    return client.post(path, data={**data, "csrf_token": token})


def count(app, table, where="1=1", params=()):
    with app.app_context():
        return get_db().execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0]


def row(app, sql, params=()):
    with app.app_context():
        return get_db().execute(sql, params).fetchone()


@pytest.fixture
def admin(client, users):
    login(client, "admin@x.com")
    return client


# ================= create student =================

def test_admin_creates_student(app, admin):
    resp = post(admin, "/students/new", VALID_STUDENT)
    assert resp.status_code == 302 and re.search(r"/students/\d+$", resp.headers["Location"])
    s = row(app, "SELECT * FROM student WHERE registration_no = 'BSSE-001'")
    assert s["email"] == "ayesha@example.com"                 # lower-cased
    assert s["is_active"] == 1
    assert s["password_hash"] != "longenough1" and check_password_hash(s["password_hash"], "longenough1")


def test_new_student_can_log_in(app, admin):
    post(admin, "/students/new", VALID_STUDENT)
    other = app.test_client()
    assert login(other, "ayesha@example.com", "longenough1").headers["Location"].endswith("/student/")


@pytest.mark.parametrize("field,value,message", [
    ("full_name", "", "Full name is required"),
    ("full_name", "A", "must be 2-100"),
    ("full_name", "12345", "must contain letters"),
    ("registration_no", "", "Registration number is required"),
    ("registration_no", "bad id!", "letters, numbers"),
    ("email", "", "Email is required"),
    ("email", "not-an-email", "valid email"),
    ("phone", "", "Phone number is required"),
    ("phone", "abc", "valid phone"),
    ("phone", "123", "valid phone"),
    ("gender", "", "Choose a gender"),
    ("gender", "Robot", "Choose a gender"),
    ("room_no", "", "Room number is required"),
    ("session", "22", "must be 4-20"),
    ("department", "", "Department is required"),
    ("parent_name", "", "Parent name is required"),
    ("parent_email", "nope", "valid parent email"),
    ("password", "", "password is required"),
    ("password", "short", "8-128"),
    ("warden_id", "9999", "Choose a warden"),
    ("warden_id", "abc", "Choose a warden"),
])
def test_invalid_student_rejected(app, admin, field, value, message):
    resp = post(admin, "/students/new", {**VALID_STUDENT, field: value})
    assert resp.status_code == 400
    assert message.lower() in resp.get_data(as_text=True).lower()
    assert "is-invalid" in resp.get_data(as_text=True)         # the field is highlighted
    assert count(app, "student") == 2                          # only the two fixture students; nothing added


def test_form_keeps_typed_values_after_error(admin):
    resp = post(admin, "/students/new", {**VALID_STUDENT, "email": "bad"})
    html = resp.get_data(as_text=True)
    assert 'value="Ayesha Khan"' in html and 'value="A-12"' in html
    assert "longenough1" not in html                           # the password is never echoed back


def test_duplicate_registration_number_rejected(app, admin):
    post(admin, "/students/new", VALID_STUDENT)
    resp = post(admin, "/students/new", {**VALID_STUDENT, "email": "other@example.com", "registration_no": "BSSE-001"})
    assert resp.status_code == 400 and b"already exists" in resp.data
    assert count(app, "student", "registration_no = 'BSSE-001'") == 1


def test_duplicate_registration_number_ignores_case(app, admin):
    post(admin, "/students/new", VALID_STUDENT)
    resp = post(admin, "/students/new", {**VALID_STUDENT, "email": "o@example.com", "registration_no": "Bsse-001"})
    assert resp.status_code == 400


@pytest.mark.parametrize("taken", ["s1@x.com", "S1@X.COM", "warden@x.com", "admin@x.com"])
def test_email_must_be_unique_across_all_account_types(app, admin, taken):
    resp = post(admin, "/students/new", {**VALID_STUDENT, "email": taken})
    assert resp.status_code == 400 and b"already used" in resp.data


def test_forged_is_active_field_is_ignored(app, admin):
    post(admin, "/students/new", {**VALID_STUDENT, "is_active": "0", "student_id": "99", "role": "admin"})
    s = row(app, "SELECT student_id, is_active FROM student WHERE registration_no = 'BSSE-001'")
    assert s["is_active"] == 1 and s["student_id"] != 99


def test_student_can_be_assigned_an_active_warden(app, admin):
    resp = post(admin, "/students/new", {**VALID_STUDENT, "warden_id": "1"})
    assert resp.status_code == 302
    assert row(app, "SELECT warden_id FROM student WHERE registration_no='BSSE-001'")["warden_id"] == 1


def test_cannot_assign_deactivated_warden(app, admin):
    with app.app_context():
        get_db().execute("UPDATE warden SET is_active = 0")
        get_db().commit()
    assert post(admin, "/students/new", {**VALID_STUDENT, "warden_id": "1"}).status_code == 400


# ================= edit / deactivate =================

def test_edit_student_saves_changes_and_keeps_password(app, admin):
    before = row(app, "SELECT password_hash FROM student WHERE student_id = 1")["password_hash"]
    data = {**VALID_STUDENT, "registration_no": "REG-001", "email": "s1@x.com", "full_name": "Renamed Student", "password": ""}
    resp = post(admin, "/students/1/edit", data)
    assert resp.status_code == 302
    after = row(app, "SELECT * FROM student WHERE student_id = 1")
    assert after["full_name"] == "Renamed Student" and after["password_hash"] == before


def test_edit_can_reset_password(app, admin):
    data = {**VALID_STUDENT, "registration_no": "REG-001", "email": "s1@x.com", "password": "brand-new-pass1"}
    post(admin, "/students/1/edit", data)
    assert login(app.test_client(), "s1@x.com", "brand-new-pass1").status_code == 302
    assert login(app.test_client(), "s1@x.com", PASSWORD).status_code == 401


def test_edit_cannot_take_another_students_email_or_number(app, admin):
    data = {**VALID_STUDENT, "registration_no": "REG-001", "email": "s2@x.com", "password": ""}
    assert post(admin, "/students/1/edit", data).status_code == 400
    data = {**VALID_STUDENT, "registration_no": "REG-002", "email": "s1@x.com", "password": ""}
    assert post(admin, "/students/1/edit", data).status_code == 400


def test_edit_form_prefilled_and_never_shows_hash(admin):
    html = admin.get("/students/1/edit").get_data(as_text=True)
    assert 'value="Student 1"' in html and "scrypt" not in html and "pbkdf2" not in html


def test_missing_student_is_404(admin):
    assert admin.get("/students/999").status_code == 404
    assert admin.get("/students/999/edit").status_code == 404
    assert post(admin, "/students/999/deactivate", {}).status_code == 404


def test_deactivate_blocks_login_and_reactivate_restores(app, admin):
    assert post(admin, "/students/1/deactivate", {}).status_code == 302
    assert row(app, "SELECT is_active FROM student WHERE student_id=1")["is_active"] == 0
    assert login(app.test_client(), "s1@x.com").status_code == 401
    assert post(admin, "/students/1/activate", {}).status_code == 302
    assert login(app.test_client(), "s1@x.com").status_code == 302


def test_deactivate_keeps_the_record_and_history(app, admin):
    with app.app_context():
        get_db().execute("INSERT INTO attendance (student_id,local_date,local_time,status) VALUES (1,'2026-01-01','10:00','Present')")
        get_db().commit()
    post(admin, "/students/1/deactivate", {})
    assert count(app, "student", "student_id = 1") == 1 and count(app, "attendance") == 1


def test_deactivated_student_is_logged_out_immediately(app, admin):
    student = app.test_client()
    login(student, "s1@x.com")
    assert student.get("/student/").status_code == 200
    post(admin, "/students/1/deactivate", {})
    assert student.get("/student/").status_code == 302


# ================= permissions =================

@pytest.mark.parametrize("method,path", [
    ("GET", "/students/new"), ("POST", "/students/new"), ("GET", "/students/1/edit"), ("POST", "/students/1/edit"),
    ("POST", "/students/1/deactivate"), ("POST", "/students/1/activate"),
    ("GET", "/wardens/"), ("GET", "/wardens/new"), ("POST", "/wardens/new"), ("GET", "/wardens/1"),
    ("GET", "/wardens/1/edit"), ("POST", "/wardens/1/edit"), ("POST", "/wardens/1/deactivate"),
])
def test_warden_cannot_change_students_or_manage_wardens(app, client, users, method, path):
    login(client, "warden@x.com")
    token = token_when_logged_in(client)
    resp = client.open(path, method=method, data={**VALID_STUDENT, "csrf_token": token})
    assert resp.status_code == 403
    assert count(app, "student") == 2 and count(app, "student", "is_active = 0") == 0


@pytest.mark.parametrize("path", ["/students/", "/students/1", "/students/new", "/students/1/edit",
                                  "/wardens/", "/wardens/1", "/wardens/new"])
def test_student_cannot_open_any_management_page(client, users, path):
    login(client, "s1@x.com")
    assert client.get(path).status_code == 403


@pytest.mark.parametrize("path", ["/students/", "/students/1", "/students/new", "/wardens/", "/wardens/new"])
def test_anonymous_is_sent_to_login(client, users, path):
    resp = client.get(path)
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]


def test_student_cannot_post_changes_even_with_a_valid_token(app, client, users):
    login(client, "s1@x.com")
    token = token_when_logged_in(client)
    for path in ("/students/new", "/students/2/edit", "/students/2/deactivate"):
        assert client.post(path, data={**VALID_STUDENT, "csrf_token": token}).status_code == 403
    assert count(app, "student") == 2


def test_post_without_csrf_token_is_rejected(app, admin):
    assert admin.post("/students/new", data=VALID_STUDENT).status_code == 400
    assert admin.post("/students/1/deactivate").status_code == 400
    assert count(app, "student") == 2 and row(app, "SELECT is_active FROM student WHERE student_id=1")["is_active"] == 1


def test_warden_can_view_student_list_and_details_but_no_edit_buttons(client, users):
    login(client, "warden@x.com")
    html = client.get("/students/").get_data(as_text=True)
    assert "Student 1" in html and "Add student" not in html and "/edit" not in html
    detail = client.get("/students/1").get_data(as_text=True)
    assert "s1@x.com" in detail and "Deactivate" not in detail and "scrypt" not in detail


def test_admin_sees_edit_and_add_buttons(admin):
    html = admin.get("/students/").get_data(as_text=True)
    assert "Add student" in html and "/students/1/edit" in html


# ================= search, filters, pagination =================

@pytest.fixture
def many(app, users):
    with app.app_context():
        db = get_db()
        for n in range(3, 28):
            dept = "Computer Science" if n % 2 else "Software Engineering"
            db.execute(
                "INSERT INTO student (registration_no,full_name,email,phone,room_no,department,session,gender,"
                "parent_name,parent_email,password_hash,is_active) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"R{n}", f"Name {n:02d}", f"s{n}@x.com", "1", f"B-{n}", dept, "2022", "F", "P", "p@x.com", "h",
                 0 if n == 27 else 1))
        db.commit()


def test_pagination(admin, many):
    html = admin.get("/students/").get_data(as_text=True)           # 26 active students -> 3 pages of 10
    assert "Page 1 of 3" in html and html.count('class="person"') == 10
    assert admin.get("/students/?page=3").get_data(as_text=True).count('class="person"') == 6
    assert "Page 3 of 3" in admin.get("/students/?page=999").get_data(as_text=True)   # clamped
    assert "Page 1 of 3" in admin.get("/students/?page=-4").get_data(as_text=True)
    assert admin.get("/students/?page=abc").status_code == 200


def test_pagination_links_keep_the_filters(admin, many):
    html = admin.get("/students/?department=Computer+Science").get_data(as_text=True)
    assert "department=Computer+Science" in html and "page=2" in html


def test_search_by_name_number_email_and_room(admin, many):
    assert b"Name 12" in admin.get("/students/?q=name+12").data
    assert b"Name 13" not in admin.get("/students/?q=name+12").data
    assert b"Name 15" in admin.get("/students/?q=R15").data
    assert b"Name 16" in admin.get("/students/?q=s16@x").data
    assert b"Name 17" in admin.get("/students/?q=B-17").data


def test_search_percent_and_underscore_are_not_wildcards(admin, many):
    for q in ("%", "_", "%%", "R_", "\\"):
        assert b"No students match your search" in admin.get("/students/", query_string={"q": q}).data


def test_search_is_safe_against_sql_injection(app, admin, many):
    for q in ("' OR '1'='1", "x'; DROP TABLE student; --", '" OR 1=1 --'):
        resp = admin.get("/students/", query_string={"q": q})
        assert resp.status_code == 200 and b"No students match your search" in resp.data
    assert count(app, "student") == 27


def test_department_and_status_filters(admin, many):
    cs = admin.get("/students/?department=Computer+Science&status=all").get_data(as_text=True)
    assert "Name 03" in cs and "Name 04" not in cs
    inactive = admin.get("/students/?status=inactive").get_data(as_text=True)
    assert "Name 27" in inactive and "Name 03" not in inactive
    assert "Name 27" not in admin.get("/students/").get_data(as_text=True)          # active is the default
    assert admin.get("/students/?status=bogus").status_code == 200                  # unknown value -> default


def test_empty_states(admin, client):
    with admin.application.app_context():
        get_db().execute("DELETE FROM student")
        get_db().commit()
    assert b"No students registered yet" in admin.get("/students/").data
    assert b"No students match your search" in admin.get("/students/?q=zzz").data


def test_names_are_escaped_in_pages(app, admin):
    post(admin, "/students/new", {**VALID_STUDENT, "full_name": "<script>alert(1)</script>"})
    for path in ("/students/?status=all", "/students/3"):
        html = admin.get(path).get_data(as_text=True)
        assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html


# ================= wardens =================

def test_admin_creates_warden_who_can_log_in(app, admin):
    resp = post(admin, "/wardens/new", VALID_WARDEN)
    assert resp.status_code == 302
    w = row(app, "SELECT * FROM warden WHERE email = 'sara@example.com'")
    assert w["is_active"] == 1 and check_password_hash(w["password_hash"], "longenough1")
    assert login(app.test_client(), "sara@example.com", "longenough1").headers["Location"].endswith("/warden/")


@pytest.mark.parametrize("field,value,message", [
    ("full_name", "", "Full name is required"), ("email", "bad", "valid email"), ("phone", "x", "valid phone"),
    ("gender", "?", "Choose a gender"), ("password", "", "password is required"), ("password", "short", "8-128"),
    ("email", "s1@x.com", "already used"),
])
def test_invalid_warden_rejected(app, admin, field, value, message):
    resp = post(admin, "/wardens/new", {**VALID_WARDEN, field: value})
    assert resp.status_code == 400 and message.lower() in resp.get_data(as_text=True).lower()
    assert count(app, "warden") == 1


def test_edit_warden(app, admin):
    resp = post(admin, "/wardens/1/edit", {**VALID_WARDEN, "email": "warden@x.com", "full_name": "Wendy Updated", "password": ""})
    assert resp.status_code == 302
    assert row(app, "SELECT full_name FROM warden WHERE warden_id=1")["full_name"] == "Wendy Updated"
    assert login(app.test_client(), "warden@x.com").status_code == 302                  # old password still works


def test_deactivate_warden_blocks_login_and_keeps_students(app, admin):
    with app.app_context():
        get_db().execute("UPDATE student SET warden_id = 1")
        get_db().commit()
    post(admin, "/wardens/1/deactivate", {})
    assert login(app.test_client(), "warden@x.com").status_code == 401
    assert count(app, "student", "warden_id = 1") == 2                                  # assignments are kept
    assert b"(deactivated)" in admin.get("/students/1").data
    post(admin, "/wardens/1/activate", {})
    assert login(app.test_client(), "warden@x.com").status_code == 302


def test_warden_list_search_and_student_count(app, admin):
    with app.app_context():
        get_db().execute("UPDATE student SET warden_id = 1 WHERE student_id = 1")
        get_db().commit()
    html = admin.get("/wardens/").get_data(as_text=True)
    assert "Wendy Warden" in html and "warden@x.com" in html
    assert b"No wardens match" in admin.get("/wardens/?q=zzz").data
    assert "Student 1" in admin.get("/wardens/1").get_data(as_text=True)
    assert "scrypt" not in admin.get("/wardens/1/edit").get_data(as_text=True)


# ================= navigation =================

def test_sidebar_links_to_management_pages(client, users):
    login(client, "admin@x.com")
    html = client.get("/admin/").get_data(as_text=True)
    assert 'href="/students/"' in html and 'href="/wardens/"' in html and "Add student" in html
    login(client := client.application.test_client(), "warden@x.com")
    html = client.get("/warden/").get_data(as_text=True)
    assert 'href="/students/"' in html and 'href="/wardens/"' not in html


def test_sidebar_highlights_section_on_subpages(admin):
    html = admin.get("/students/1/edit").get_data(as_text=True)
    assert 'href="/students/" class="nav-link-item active"' in html


def test_confirmation_markup_present_on_deactivate(admin):
    html = admin.get("/students/1").get_data(as_text=True)
    assert 'data-confirm="Deactivate Student 1?' in html and 'id="confirmModal"' in html
