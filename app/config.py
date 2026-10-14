"""All settings in one place (SRS NFR-08). Values come from environment variables / .env."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


class Config:
    # Used to sign session cookies. Must be set to a long random value in .env.
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-insecure-key")

    DATABASE_PATH = os.environ.get("DATABASE_PATH", str(BASE_DIR / "instance" / "hostel.db"))

    TIMEZONE = os.environ.get("TIMEZONE", "Asia/Karachi")
    CURFEW_TIME = os.environ.get("CURFEW_TIME", "22:00")
    # The attendance "hostel day" starts at this local time, so a 00:30 arrival counts for the previous day (as Late).
    ATTENDANCE_DAY_START = os.environ.get("ATTENDANCE_DAY_START", "04:00")

    # Session cookie safety
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    # Set SESSION_COOKIE_SECURE=1 once the site is served over HTTPS (plain http://localhost needs 0).
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "0") == "1"
    PERMANENT_SESSION_LIFETIME = 15 * 60  # idle timeout: 15 minutes (SRS NFR-03)

    # --- Guest visits (Phase 9) ---
    GUEST_MAX_HOURS = 12            # longest allowed validity window of one visit
    GUEST_MAX_DAYS_AHEAD = 30       # how far in advance a visit may be requested
    GUEST_MAX_PENDING = 5           # open requests one student may have at the same time
    GUEST_MAX_ENTRIES = 3           # the most entries a warden can allow for one visit (default is 1: one-time)

    # --- Alerts and email (Phase 10) ---
    # Automatic security alert: this many counted failures within this many minutes (see docs/alerts-and-email.md for what is counted).
    ALERT_FAILED_THRESHOLD = int(os.environ.get("ALERT_FAILED_THRESHOLD", "3"))
    ALERT_WINDOW_MINUTES = int(os.environ.get("ALERT_WINDOW_MINUTES", "5"))
    EMERGENCY_COOLDOWN_SECONDS = 120        # a second emergency press within this time is not a new alert
    EMAIL_SEND_ASYNC = True                 # send in a background thread so a slow mail server never blocks a page
    EMAIL_MAX_ATTEMPTS = 5                  # per message, then it stays Failed until the next weekly re-queue
    SCHEDULER_ENABLED = os.environ.get("SCHEDULER_ENABLED", "1") == "1"
    WEEKLY_REPORT_DAY = os.environ.get("WEEKLY_REPORT_DAY", "sun")      # mon..sun
    WEEKLY_REPORT_TIME = os.environ.get("WEEKLY_REPORT_TIME", "20:00")  # local time, HH:MM
    HOSTEL_NAME = os.environ.get("HOSTEL_NAME", "Hostel Security System")

    # LOCAL TESTING ONLY. When 1, the login page shows one-click buttons that sign in as the demo admin / warden /
    # student without a password. It only works from the computer running the app and must stay 0 anywhere else.
    DEV_QUICK_LOGIN = os.environ.get("DEV_QUICK_LOGIN", "0") == "1"
    DEV_ACCOUNTS = {"admin": "admin@hostel.local", "warden": "warden@hostel.local", "student": "student@hostel.local"}

    # Biometric templates are encrypted with this Fernet key before they reach the database.
    # Create one with:  flask --app run generate-biometric-key   (keep it secret and BACK IT UP:
    # without the key, enrolled templates can no longer be read).
    BIOMETRIC_KEY = os.environ.get("BIOMETRIC_KEY", "")

    # Recognition thresholds (tuned in Phase 7)
    FACE_THRESHOLD = float(os.environ.get("FACE_THRESHOLD", "0.5"))

    # --- Entry verification (Phase 7) ---
    # Who may use the entry terminal without logging in. Only the gate computer itself by default (SRS 3.3).
    TERMINAL_ADDRESSES = [a.strip() for a in os.environ.get("TERMINAL_ADDRESSES", "127.0.0.1,::1").split(",") if a.strip()]
    FACE_MARGIN = float(os.environ.get("FACE_MARGIN", "0.04"))       # two students this close => inconclusive, deny
    # Blink liveness: a blink is a dip of the eye aspect ratio (EAR) below DROP * normal, with open eyes before and after.
    LIVENESS_MIN_FRAMES = 10
    LIVENESS_MAX_FRAMES = 40
    # Faces are found at this size. Smaller is faster but the eye landmarks get noisy: with the same blink we measured a
    # closed-eye EAR of 0.15 at 640/560 px but 0.25 at 480 px. The camera sends 640 px, so nothing is shrunk by default.
    LIVENESS_WORK_SIDE = int(os.environ.get("LIVENESS_WORK_SIDE", "640"))
    FACE_FRAME_RATIO = 0.6                                           # share of frames that must show exactly one face
    MULTI_FACE_RATIO = 0.2                                           # more than this share with 2+ faces => deny
    BLINK_DROP_RATIO = float(os.environ.get("BLINK_DROP_RATIO", "0.78"))
    BLINK_OPEN_RATIO = 0.90
    BLINK_CLOSED_MAX_EAR = float(os.environ.get("BLINK_CLOSED_MAX_EAR", "0.25"))
    BLINK_MAX_CLOSED_FRAMES = 8
    EAR_MIN_BASELINE = 0.18
    # Eye-region fallback. OFF by default: in our measurements the eye-region template could not separate
    # different people reliably (see docs/recognition-limits.md). When ON it can only CONFIRM a borderline face
    # match for the same student, never identify anyone by itself, and every such entry is flagged for review.
    EYE_FALLBACK_ENABLED = os.environ.get("EYE_FALLBACK_ENABLED", "0") == "1"
    EYE_THRESHOLD = float(os.environ.get("EYE_THRESHOLD", "0.06"))   # chi-square distance, smaller = more alike
    EYE_MARGIN = float(os.environ.get("EYE_MARGIN", "0.05"))
    FACE_FALLBACK_MAX = float(os.environ.get("FACE_FALLBACK_MAX", "0.6"))   # face must at least be this close

    # Enrollment: how many samples, and how strict the image checks are.
    ENROLLMENT_SAMPLES = 5
    MAX_CONTENT_LENGTH = 12 * 1024 * 1024        # largest request body Flask accepts
    MAX_IMAGE_BYTES = 2 * 1024 * 1024            # largest single image
    MIN_IMAGE_SIDE = 240                         # pixels
    MAX_IMAGE_SIDE = 4096                        # pixels (guards against decompression bombs)
    WORK_IMAGE_SIDE = 960                        # images are shrunk to this before processing
    FACE_MIN_PX = int(os.environ.get("FACE_MIN_PX", "90"))            # face box width, in pixels
    BRIGHTNESS_MIN = float(os.environ.get("BRIGHTNESS_MIN", "45"))    # mean of the face area, 0-255
    BRIGHTNESS_MAX = float(os.environ.get("BRIGHTNESS_MAX", "215"))
    SHARPNESS_MIN = float(os.environ.get("SHARPNESS_MIN", "30"))      # Laplacian variance of the face area
    ENROLL_CONSISTENCY_MAX = float(os.environ.get("ENROLL_CONSISTENCY_MAX", "0.5"))  # samples must be one person

    # Email (optional)
    MAIL_SERVER = os.environ.get("MAIL_SERVER", "")
    MAIL_PORT = int(os.environ.get("MAIL_PORT", "587"))
    MAIL_USE_TLS = os.environ.get("MAIL_USE_TLS", "1") == "1"
    MAIL_USERNAME = os.environ.get("MAIL_USERNAME", "")
    MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD", "")
    MAIL_DEFAULT_SENDER = os.environ.get("MAIL_DEFAULT_SENDER", "")
