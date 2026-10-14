"""Alert, contact and email pages; the emergency button; the terminal raising a security alert."""
import io
import smtplib

import pytest

from app.db import get_db
from app.services import alerts, emailer
from app.services import verification as V
from conftest import login, token_when_logged_in
from test_alerts_logic import PASSWORD, add_contacts, fail_send, mailapp, outbox  # noqa: F401  (mailapp is a fixture)


def as_(app, email):
    c = app.test_client()
    login(c, email)
    return c


def student(app, email="s1@x.com"):
    return as_(app, email)


def warden(app):
    return as_(app, "warden@x.com")


def admin(app):
    return as_(app, "admin@x.com")


def post(client, path, **data):
    return client.post(path, data={**data, "csrf_token": token_when_logged_in(client)})


def rows(app, sql, params=()):
    with app.app_context():
        return get_db().execute(sql, params).fetchall()


def count(app, table, where="1=1"):
    return rows(app, f"SELECT COUNT(*) FROM {table} WHERE {where}")[0][0]


# ================= the emergency button =================

def test_dashboard_and_alert_page_have_a_confirmed_emergency_button(app, users):
    c = student(app)
    for path in ("/student/", "/alerts/my"):
        html = c.get(path).get_data(as_text=True)
        assert "Emergency" in html and 'data-confirm="Send an emergency alert' in html and 'action="/alerts/emergency"' in html
        assert "does not call the police" in html or "do <strong>not</strong> call the police" in html or "not</strong> call the police" in html


def test_student_presses_emergency(app, users):
    c = student(app)
    resp = post(c, "/alerts/emergency")
    assert resp.status_code == 302
    (a,) = rows(app, "SELECT * FROM alert")
    assert (a["student_id"], a["alert_type"], a["status"]) == (1, "Emergency", "New")
    page = c.get(resp.headers["Location"]).get_data(as_text=True)
    assert "emergency alert was sent to the warden" in page and "does not call the police" in page
    assert "New" in c.get("/alerts/my").get_data(as_text=True)


def test_pressing_twice_sends_one_alert(app, users):
    c = student(app)
    post(c, "/alerts/emergency")
    page = c.get(post(c, "/alerts/emergency").headers["Location"]).get_data(as_text=True)
    assert count(app, "alert") == 1 and "sent a moment ago" in page


def test_a_student_cannot_raise_an_alert_for_someone_else(app, users):
    c = student(app, "s1@x.com")
    post(c, "/alerts/emergency", student_id="2", student="2", alert_type="Unauthorized Attempt", status="Acknowledged", message="fake")
    (a,) = rows(app, "SELECT * FROM alert")
    assert (a["student_id"], a["alert_type"], a["status"]) == (1, "Emergency", "New") and "fake" not in a["message"]


@pytest.mark.parametrize("who", ["warden@x.com", "admin@x.com"])
def test_only_students_can_press_the_button(app, users, who):
    assert post(as_(app, who), "/alerts/emergency").status_code == 403 and count(app, "alert") == 0


def test_the_button_needs_a_login_and_a_csrf_token(app, users):
    anonymous = app.test_client()
    anonymous.get("/login")
    assert anonymous.post("/alerts/emergency").status_code == 400
    c = student(app)
    c.get("/student/")
    assert c.post("/alerts/emergency").status_code == 400 and count(app, "alert") == 0


def test_students_see_only_their_own_alerts(app, users):
    post(student(app, "s1@x.com"), "/alerts/emergency")
    post(student(app, "s2@x.com"), "/alerts/emergency")
    with app.app_context():
        get_db().execute("UPDATE alert SET status='Acknowledged', acknowledged_by_warden_id=1, acknowledged_at='2026-10-14T10:00:00' WHERE student_id = 2")
        get_db().commit()
    one, two = student(app, "s1@x.com").get("/alerts/my").get_data(as_text=True), student(app, "s2@x.com").get("/alerts/my").get_data(as_text=True)
    assert one.count("<tr>") == 2 and "Acknowledged" not in one                      # header + one New row
    assert "Acknowledged" in two and "Wendy Warden" in two


def test_students_cannot_open_the_staff_alert_list(app, users):
    assert student(app).get("/alerts/").status_code == 403


def test_emergency_emails_are_queued_and_a_mail_failure_loses_nothing(mailapp, users, monkeypatch):
    add_contacts(mailapp)
    fail_send(monkeypatch)
    c = student(mailapp)
    resp = post(c, "/alerts/emergency")
    assert resp.status_code == 302                                                    # the student is not told about mail problems
    assert count(mailapp, "alert") == 1
    assert {r["status"] for r in outbox(mailapp, "emergency")} == {"Failed"} and len(outbox(mailapp, "emergency")) == 4
    assert "New" in warden(mailapp).get("/alerts/").get_data(as_text=True)            # the warden still sees it


def test_emergency_emails_are_sent_when_a_server_is_configured(mailapp, users):
    add_contacts(mailapp)
    with emailer.mail.record_messages() as sent:
        post(student(mailapp), "/alerts/emergency")
    assert sorted(r for m in sent for r in m.recipients) == ["other@x.com", "police@x.com", "security@x.com", "warden@x.com"]
    assert all("Student 1" in m.body for m in sent)


def test_without_a_mail_server_the_alert_still_works(app, users):
    post(student(app), "/alerts/emergency")
    assert count(app, "alert") == 1 and {r["status"] for r in outbox(app)} == {"Skipped"}
    assert "Student 1" in warden(app).get("/warden/").get_data(as_text=True)


# ================= wardens see and acknowledge =================

def raise_alert(app, student_id=1):
    with app.app_context():
        return alerts.create_emergency(get_db(), get_db().execute("SELECT * FROM student WHERE student_id = ?", (student_id,)).fetchone(), app.config)[1]


def raise_security(app):
    with app.app_context():
        get_db().execute("INSERT INTO alert (alert_type, message, created_at, status) VALUES ('Unauthorized Attempt', '3 failed entry attempts by unidentified people', '2026-10-14T10:00:00', 'New')")
        get_db().commit()


def test_warden_dashboard_shows_both_kinds_with_acknowledge_buttons(app, users):
    raise_alert(app)
    raise_security(app)
    html = warden(app).get("/warden/").get_data(as_text=True)
    assert "Student 1" in html and "3 failed entry attempts" in html and html.count(">Acknowledge<") == 2


def test_warden_acknowledges_from_the_dashboard(app, users):
    alert_id = raise_alert(app)
    c = warden(app)
    resp = post(c, f"/alerts/{alert_id}/acknowledge", next="dashboard")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/warden/")
    (a,) = rows(app, "SELECT * FROM alert")
    assert (a["status"], a["acknowledged_by_warden_id"]) == ("Acknowledged", 1) and a["acknowledged_at"]
    assert ">Acknowledge<" not in c.get("/warden/").get_data(as_text=True).split("Security alerts")[0]


def test_warden_acknowledges_from_the_list_and_it_shows_who_and_when(app, users):
    alert_id = raise_alert(app)
    c = warden(app)
    assert post(c, f"/alerts/{alert_id}/acknowledge").headers["Location"].endswith("/alerts/")
    html = c.get("/alerts/").get_data(as_text=True)
    assert "Acknowledged" in html and "by Wendy Warden" in html and ">Acknowledge<" not in html


def test_acknowledging_twice_changes_nothing(app, users):
    alert_id = raise_alert(app)
    c = warden(app)
    post(c, f"/alerts/{alert_id}/acknowledge")
    first = rows(app, "SELECT acknowledged_at FROM alert")[0][0]
    page = c.get(post(c, f"/alerts/{alert_id}/acknowledge").headers["Location"]).get_data(as_text=True)
    assert "already acknowledged" in page and rows(app, "SELECT acknowledged_at FROM alert")[0][0] == first


@pytest.mark.parametrize("who", ["admin@x.com", "s1@x.com"])
def test_only_wardens_can_acknowledge(app, users, who):
    alert_id = raise_alert(app)
    assert post(as_(app, who), f"/alerts/{alert_id}/acknowledge").status_code == 403
    assert rows(app, "SELECT status FROM alert")[0][0] == "New"


def test_acknowledging_needs_csrf_and_a_known_alert(app, users):
    alert_id = raise_alert(app)
    c = warden(app)
    c.get("/alerts/")
    assert c.post(f"/alerts/{alert_id}/acknowledge").status_code == 400 and rows(app, "SELECT status FROM alert")[0][0] == "New"
    assert "does not exist" in c.get(post(c, "/alerts/999/acknowledge").headers["Location"]).get_data(as_text=True)


def test_admin_sees_alerts_but_has_no_buttons(app, users):
    raise_alert(app)
    html = admin(app).get("/alerts/").get_data(as_text=True)
    assert "Student 1" in html and ">Acknowledge<" not in html and "<th class=\"text-end\">Action</th>" not in html


def test_sidebar_badge_counts_new_alerts(app, users):
    alert_id = raise_alert(app)
    raise_security(app)
    c = warden(app)
    assert 'class="nav-badge" aria-label="2 new">2<' in c.get("/warden/").get_data(as_text=True)
    post(c, f"/alerts/{alert_id}/acknowledge")
    assert 'aria-label="1 new">1<' in c.get("/warden/").get_data(as_text=True)
    assert "nav-badge" not in student(app).get("/student/").get_data(as_text=True)


def test_alert_list_filters_and_empty_state(app, users):
    ack = raise_alert(app, 1)
    raise_alert(app, 2)
    raise_security(app)
    c = warden(app)
    post(c, f"/alerts/{ack}/acknowledge")
    assert c.get("/alerts/?type=Emergency").get_data(as_text=True).count("Emergency</span>") == 2
    assert "by unidentified people" not in c.get("/alerts/?type=Emergency").get_data(as_text=True)     # the security alert is filtered out
    new_only = c.get("/alerts/?status=New").get_data(as_text=True)
    assert "Student 1" not in new_only and "Student 2" in new_only
    assert c.get("/alerts/?from=2020-01-01&to=2020-01-02").get_data(as_text=True).count("No alerts") >= 1
    assert c.get("/alerts/?type=bogus&status=bogus&page=-1&from=x").status_code == 200


def test_new_alerts_are_listed_first(app, users):
    old = raise_alert(app, 1)
    c = warden(app)
    post(c, f"/alerts/{old}/acknowledge")
    raise_alert(app, 2)
    html = c.get("/alerts/").get_data(as_text=True)
    assert html.index("Student 2") < html.index("Student 1")


def test_alert_pages_are_safe(app, users):
    c = warden(app)
    for q in ("' OR 1=1 --", "<script>alert(1)</script>"):
        assert c.get("/alerts/", query_string={"type": q, "status": q}).status_code == 200
    with app.app_context():
        get_db().execute("UPDATE student SET full_name = '<script>alert(1)</script>' WHERE student_id = 1")
        get_db().commit()
    raise_alert(app)
    assert b"<script>alert(1)</script>" not in c.get("/alerts/").data


def test_alert_page_explains_the_rule(app, users):
    assert "3 failed entry attempts" in warden(app).get("/alerts/").get_data(as_text=True)


# ================= the terminal raises a security alert =================

def gate(app, monkeypatch, decision):
    monkeypatch.setattr(V, "verify", lambda frames, faces, eyes, settings, **kw: decision)
    c = app.test_client()
    c.get("/terminal/")
    with c.session_transaction() as s:
        token = s["_csrf"]
    return c.post("/terminal/verify", content_type="multipart/form-data", headers={"X-CSRF-Token": token},
                  data={"frames": [(io.BytesIO(b"x"), f"f{i}.jpg") for i in range(12)]})


def failed(code="not_recognized"):
    return V.deny(code, liveness="passed" if code != "liveness_failed" else "failed", distance=0.7)


def test_three_failures_at_the_gate_raise_an_alert_for_the_warden(app, users, monkeypatch):
    for _ in range(2):
        gate(app, monkeypatch, failed())
    assert count(app, "alert") == 0
    gate(app, monkeypatch, failed("liveness_failed"))
    (a,) = rows(app, "SELECT * FROM alert")
    assert a["alert_type"] == "Unauthorized Attempt" and a["student_id"] is None and a["status"] == "New"
    html = warden(app).get("/warden/").get_data(as_text=True)
    assert "3 failed entry attempts" in html and ">Acknowledge<" in html
    assert count(app, "entry_log", "result = 'Denied'") == 3


def test_more_failures_in_the_same_incident_do_not_add_alerts(app, users, monkeypatch):
    for _ in range(7):
        gate(app, monkeypatch, failed())
    assert count(app, "alert") == 1


def test_camera_problems_do_not_raise_alerts(app, users, monkeypatch):
    for code in ("no_face", "blurry", "too_dark", "bad_frames"):
        for _ in range(3):
            gate(app, monkeypatch, V.deny(code))
    assert count(app, "alert") == 0


def test_the_security_email_goes_to_wardens_and_security_contacts(mailapp, users, monkeypatch):
    add_contacts(mailapp)
    with emailer.mail.record_messages() as sent:
        for _ in range(3):
            gate(mailapp, monkeypatch, failed())
    assert sorted(r for m in sent for r in m.recipients) == ["security@x.com", "warden@x.com"]
    assert "Security alert" in sent[0].subject and "no student is named" in sent[0].body


def test_a_mail_failure_does_not_stop_the_gate_or_lose_the_alert(mailapp, users, monkeypatch):
    fail_send(monkeypatch)
    last = None
    for _ in range(3):
        last = gate(mailapp, monkeypatch, failed())
    assert last.status_code == 200 and last.get_json()["granted"] is False
    assert count(mailapp, "alert") == 1 and {r["status"] for r in outbox(mailapp, "security")} == {"Failed"}


def test_the_log_row_and_its_alert_are_saved_together(app, users, monkeypatch):
    for _ in range(2):
        gate(app, monkeypatch, failed())
    monkeypatch.setattr(alerts, "check_failed_attempts", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("alert code broke")))
    resp = gate(app, monkeypatch, failed())
    assert resp.status_code == 500 and not resp.get_json().get("granted")              # fails closed
    assert count(app, "entry_log") == 2                                                 # the third row was rolled back with it


# ================= emergency contacts =================

CONTACT = {"contact_name": "Hostel Security Office", "contact_type": "Security", "phone": "+92 61 1234567", "email": "Security@Example.com"}


@pytest.mark.parametrize("path", ["/contacts/", "/contacts/new"])
def test_contacts_are_admin_only(app, users, path):
    assert admin(app).get(path).status_code == 200
    assert warden(app).get(path).status_code == 403 and student(app).get(path).status_code == 403
    assert app.test_client().get(path).status_code == 302


def test_admin_adds_edits_and_deletes_a_contact(app, users):
    c = admin(app)
    assert post(c, "/contacts/new", **CONTACT).status_code == 302
    (row,) = rows(app, "SELECT * FROM emergency_contact")
    assert (row["contact_name"], row["contact_type"], row["email"], row["admin_id"]) == ("Hostel Security Office", "Security", "security@example.com", 1)
    assert "Hostel Security Office" in c.get("/contacts/").get_data(as_text=True)
    assert post(c, f"/contacts/{row['contact_id']}/edit", **{**CONTACT, "contact_name": "Renamed", "contact_type": "Police"}).status_code == 302
    assert rows(app, "SELECT contact_name, contact_type FROM emergency_contact")[0][:] == ("Renamed", "Police")
    assert post(c, f"/contacts/{row['contact_id']}/delete").status_code == 302 and count(app, "emergency_contact") == 0


@pytest.mark.parametrize("field,value,message", [
    ("contact_name", "", "name"), ("contact_name", "A", "name"), ("contact_type", "", "type"), ("contact_type", "Mafia", "type"),
    ("phone", "", "phone"), ("phone", "abc", "phone"), ("phone", "123", "phone"), ("email", "", "email"), ("email", "nope", "email"),
])
def test_invalid_contacts_are_rejected(app, users, field, value, message):
    resp = post(admin(app), "/contacts/new", **{**CONTACT, field: value})
    assert resp.status_code == 400 and "is-invalid" in resp.get_data(as_text=True) and count(app, "emergency_contact") == 0


def test_contact_changes_need_admin_and_csrf(app, users):
    post(admin(app), "/contacts/new", **CONTACT)
    cid = rows(app, "SELECT contact_id FROM emergency_contact")[0][0]
    for who in (warden(app), student(app)):
        assert post(who, "/contacts/new", **CONTACT).status_code == 403
        assert post(who, f"/contacts/{cid}/delete").status_code == 403
    c = admin(app)
    c.get("/contacts/")
    assert c.post(f"/contacts/{cid}/delete").status_code == 400 and count(app, "emergency_contact") == 1
    assert c.get("/contacts/999/edit").status_code == 404 and post(c, "/contacts/999/delete").status_code == 404


def test_contact_search_and_empty_state(app, users):
    c = admin(app)
    assert "No emergency contacts yet" in c.get("/contacts/").get_data(as_text=True)
    post(c, "/contacts/new", **CONTACT)
    assert "Hostel Security Office" in c.get("/contacts/?q=security").get_data(as_text=True)
    assert "No contacts match" in c.get("/contacts/?q=zzz").get_data(as_text=True)
    assert c.get("/contacts/", query_string={"q": "' OR 1=1 --"}).status_code == 200


def test_a_deleted_contact_no_longer_gets_emails(app, users):
    c = admin(app)
    post(c, "/contacts/new", **CONTACT)
    post(c, f"/contacts/{rows(app, 'SELECT contact_id FROM emergency_contact')[0][0]}/delete")
    post(student(app), "/alerts/emergency")
    assert [r["recipient"] for r in outbox(app, "emergency")] == ["warden@x.com"]


# ================= the email page =================

def test_email_page_is_admin_only(app, users):
    assert admin(app).get("/emails/").status_code == 200
    assert warden(app).get("/emails/").status_code == 403 and student(app).get("/emails/").status_code == 403


def test_email_page_shows_the_server_but_never_the_password(mailapp, users):
    html = admin(mailapp).get("/emails/").get_data(as_text=True)
    assert "smtp.test" in html and "Configured" in html and "password hidden" in html
    assert PASSWORD not in html and "hostel-user" not in html


def test_email_page_without_a_server_explains_what_to_do(app, users):
    html = admin(app).get("/emails/").get_data(as_text=True)
    assert "Not configured" in html and "MAIL_SERVER" in html and "No emails yet" in html


def test_test_email_button(mailapp, users):
    c = admin(mailapp)
    with emailer.mail.record_messages() as sent:
        page = c.get(post(c, "/emails/test", recipient="me@x.com").headers["Location"]).get_data(as_text=True)
    assert "test email was sent to me@x.com" in page and sent[0].recipients == ["me@x.com"]


def test_test_email_problems_are_shown_without_secrets(mailapp, users, monkeypatch):
    c = admin(mailapp)
    fail_send(monkeypatch, smtplib.SMTPAuthenticationError(535, f"bad {PASSWORD}".encode()))
    page = c.get(post(c, "/emails/test", recipient="me@x.com").headers["Location"]).get_data(as_text=True)
    assert "could not be sent" in page and PASSWORD not in page


def test_test_email_validation_unconfigured_and_permissions(app, mailapp, users):
    c = admin(mailapp)
    assert "valid email" in c.get(post(c, "/emails/test", recipient="nope").headers["Location"]).get_data(as_text=True)
    mailapp.config["MAIL_SERVER"] = ""
    assert "No mail server" in c.get(post(c, "/emails/test", recipient="me@x.com").headers["Location"]).get_data(as_text=True)
    assert post(warden(app), "/emails/test", recipient="me@x.com").status_code == 403
    c.get("/emails/")
    assert c.post("/emails/test", data={"recipient": "me@x.com"}).status_code == 400


def test_email_log_filter_and_retry(mailapp, users, monkeypatch):
    post(student(mailapp), "/alerts/emergency")                       # sent
    with monkeypatch.context() as m:
        fail_send(m)
        post(student(mailapp, "s2@x.com"), "/alerts/emergency")      # failed
    c = admin(mailapp)
    failed_only = c.get("/emails/?status=Failed").get_data(as_text=True)
    assert "Student 2" in failed_only and "SMTPServerDisconnected" in failed_only and "Student 1" not in failed_only
    assert c.get("/emails/?status=bogus").status_code == 200
    with emailer.mail.record_messages() as sent:
        page = c.get(post(c, "/emails/retry").headers["Location"]).get_data(as_text=True)
    assert "queued to be sent again" in page and len(sent) == 1
    assert rows(mailapp, "SELECT COUNT(*) FROM email_outbox WHERE status = 'Failed'")[0][0] == 0
    assert post(warden(mailapp), "/emails/retry").status_code == 403


# ================= menus =================

def test_menus_are_live(app, users):
    a = admin(app).get("/admin/").get_data(as_text=True)
    for path in ("/alerts/", "/contacts/", "/emails/"):
        assert f'href="{path}"' in a
    assert 'href="/alerts/"' in warden(app).get("/warden/").get_data(as_text=True)
    assert 'href="/contacts/"' not in warden(app).get("/warden/").get_data(as_text=True)
    assert 'href="/alerts/my"' in student(app).get("/student/").get_data(as_text=True)