"""Final tuning: fewer false rejects at the gate, and mail settings that survive copy-paste mistakes."""
import numpy as np

from app import create_app
from app.config import Config
from app.services import emailer
from app.services import verification as V

S = V.Settings()


def _app(tmp_path, **extra):
    return create_app({"TESTING": True, "SECRET_KEY": "k", "DATABASE_PATH": str(tmp_path / "t.db"), "SCHEDULER_ENABLED": False, **extra})


# ---------- the gate is more forgiving than enrollment, but not about who the person is ----------

def test_gate_accepts_softer_pictures_than_enrollment(tmp_path):
    app = _app(tmp_path)
    assert app.config["SHARPNESS_MIN"] == 30                      # enrollment is still strict
    assert V.Settings.from_config(app.config).limits.sharpness_min == 12


def test_a_soft_webcam_is_never_stricter_than_enrollment(tmp_path):
    app = _app(tmp_path, SHARPNESS_MIN=8, VERIFY_SHARPNESS_MIN=12)
    assert V.Settings.from_config(app.config).limits.sharpness_min == 8


def test_several_frames_let_one_good_frame_rescue_a_real_student():
    base = np.zeros(128)
    far, near = base.copy(), base.copy()
    far[0], near[0] = 0.62, 0.30                                  # one unlucky frame, one good frame
    assert not V.decide(far, None, {1: [base]}, {}, S).granted
    assert V.decide(np.array([far, near]), None, {1: [base]}, {}, S).granted


def test_several_frames_do_not_let_a_stranger_in():
    base = np.zeros(128)
    stranger = base.copy()
    stranger[0] = 0.75                                            # different people measured 0.71-0.90
    assert not V.decide(np.array([stranger, stranger * 0.99]), None, {1: [base]}, {}, S).granted


def test_two_close_students_are_still_ambiguous_with_several_frames():
    a, b = np.zeros(128), np.zeros(128)
    b[0] = 0.01
    probe = np.array([np.full(128, 0.0), np.full(128, 0.0)])
    assert V.decide(probe, None, {1: [a], 2: [b]}, {}, S).code == "ambiguous"


def test_a_slow_gentle_blink_counts():
    ears = [0.30] * 8 + [0.25, 0.23] + [0.30] * 8                 # ~25% dip, two frames
    assert V.blink_detected(ears, S)[0]


# ---------- mail settings ----------

def test_app_password_spaces_are_removed(monkeypatch):
    monkeypatch.setenv("MAIL_PASSWORD", "abcd efgh ijkl mnop")
    import importlib, app.config as cfg
    try:
        importlib.reload(cfg)
        assert cfg.Config.MAIL_PASSWORD == "abcdefghijklmnop"
    finally:
        monkeypatch.undo()
        importlib.reload(cfg)


def test_port_465_means_ssl_not_starttls(monkeypatch):
    monkeypatch.setenv("MAIL_PORT", "465")
    monkeypatch.setenv("MAIL_USE_TLS", "1")
    monkeypatch.delenv("MAIL_USE_SSL", raising=False)
    import importlib, app.config as cfg
    try:
        importlib.reload(cfg)
        assert cfg.Config.MAIL_USE_SSL is True and cfg.Config.MAIL_USE_TLS is False
    finally:
        monkeypatch.undo()
        importlib.reload(cfg)


def test_sender_defaults_to_the_login_address(monkeypatch):
    monkeypatch.setenv("MAIL_USERNAME", "hostel@gmail.com")
    monkeypatch.setenv("MAIL_DEFAULT_SENDER", "")             # empty, so a real .env value cannot leak into the test
    import importlib, app.config as cfg
    try:
        importlib.reload(cfg)
        assert cfg.Config.MAIL_DEFAULT_SENDER == "hostel@gmail.com"
        assert emailer.smtp_configured({"MAIL_SERVER": "smtp.gmail.com", "MAIL_DEFAULT_SENDER": cfg.Config.MAIL_DEFAULT_SENDER})
    finally:
        monkeypatch.undo()
        importlib.reload(cfg)


def test_a_wrong_gmail_password_gets_a_useful_hint():
    import smtplib
    text = emailer.clean_error(smtplib.SMTPAuthenticationError(535, b"bad"), {"MAIL_PASSWORD": "x", "MAIL_USERNAME": "y"})
    assert "App Password" in text and len(text) <= 200


def test_mail_connection_has_a_timeout(tmp_path):
    app = _app(tmp_path, MAIL_TIMEOUT=7)
    assert app.config["MAIL_TIMEOUT"] == 7
    assert app.extensions["mail"].timeout == 7
