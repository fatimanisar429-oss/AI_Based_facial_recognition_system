"""Shared test setup: a fresh app + empty database for every test."""
import pytest
from cryptography.fernet import Fernet
from werkzeug.security import generate_password_hash

from app import create_app
from app.db import get_db

PASSWORD = "correct-horse-1"


@pytest.fixture
def app(tmp_path):
    return create_app({
        "TESTING": True,
        "SECRET_KEY": "test-secret",
        "DATABASE_PATH": str(tmp_path / "test.db"),
        "BIOMETRIC_KEY": Fernet.generate_key().decode(),   # a fresh key for every test
        "EMAIL_SEND_ASYNC": False,                         # tests send (or fail to send) mail on the spot
        "SCHEDULER_ENABLED": False,
        "MAIL_SERVER": "", "MAIL_DEFAULT_SENDER": "",   # no mail server unless a test configures one
        "DEV_QUICK_LOGIN": False,                          # the real .env may switch it on; tests must not depend on that
        "TIMEZONE": "Asia/Karachi",                        # fixed so results do not depend on the developer's .env
        "CURFEW_TIME": "22:00",
        "ATTENDANCE_DAY_START": "04:00",
    })


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def users(app):
    """One admin, one warden and two students, all with PASSWORD."""
    h = generate_password_hash(PASSWORD)
    with app.app_context():
        db = get_db()
        db.execute("INSERT INTO admin (full_name,email,phone,password_hash) VALUES ('Ada Admin','admin@x.com','1',?)", (h,))
        db.execute("INSERT INTO warden (full_name,email,phone,gender,password_hash) VALUES ('Wendy Warden','warden@x.com','1','F',?)", (h,))
        for n in (1, 2):
            db.execute(
                "INSERT INTO student (registration_no,full_name,email,phone,room_no,department,session,gender,"
                "parent_name,parent_email,password_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (f"REG-00{n}", f"Student {n}", f"s{n}@x.com", "1", "1", "CS", "2022", "F", "P", "p@x.com", h),
            )
        db.commit()


def get_token(client):
    """Load the login page and return the CSRF token the server put in the session."""
    client.get("/login")
    with client.session_transaction() as s:
        return s["_csrf"]


def token_when_logged_in(client):
    """CSRF token for a logged-in client. Login wipes the session, so render any page to get a new one."""
    client.get("/", follow_redirects=True)
    with client.session_transaction() as s:
        return s["_csrf"]


def login(client, email, password=PASSWORD):
    token = get_token(client)
    return client.post("/login", data={"email": email, "password": password, "csrf_token": token})
