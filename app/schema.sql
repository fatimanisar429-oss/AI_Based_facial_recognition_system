-- Database schema (SQLite). Follows SRS section 2.9, plus the guest/exit tables from the project prompt.
-- Every statement is "IF NOT EXISTS", so running it again never deletes data.
-- Timestamps are stored as UTC text (ISO 8601). "local_date" columns hold the date in the configured timezone.

CREATE TABLE IF NOT EXISTS admin (
    admin_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name     TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE,
    phone         TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    is_active     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS warden (
    warden_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name     TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE,
    phone         TEXT NOT NULL,
    gender        TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    is_active     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS student (
    student_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    registration_no TEXT NOT NULL UNIQUE,
    full_name       TEXT NOT NULL,
    email           TEXT NOT NULL UNIQUE,
    phone           TEXT NOT NULL,
    room_no         TEXT NOT NULL,
    department      TEXT NOT NULL,
    session         TEXT NOT NULL,
    gender          TEXT NOT NULL,
    parent_name     TEXT NOT NULL,
    parent_email    TEXT NOT NULL,
    password_hash   TEXT NOT NULL,
    warden_id       INTEGER REFERENCES warden(warden_id),
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT                      -- when the account was registered (UTC); NULL for accounts that predate this column
);
CREATE INDEX IF NOT EXISTS idx_student_name ON student(full_name);

CREATE TABLE IF NOT EXISTS guest_profile (
    guest_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name       TEXT NOT NULL,
    contact         TEXT NOT NULL,
    host_student_id INTEGER NOT NULL REFERENCES student(student_id),
    created_at      TEXT NOT NULL
);

-- One row per stored sample template. Templates are numbers, never photographs.
CREATE TABLE IF NOT EXISTS biometric_profile (
    profile_id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_type TEXT NOT NULL CHECK (owner_type IN ('student', 'guest')),
    owner_id   INTEGER NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('face', 'eye')),
    sample_no  INTEGER NOT NULL,
    template   BLOB NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (owner_type, owner_id, kind, sample_no)
);

CREATE TABLE IF NOT EXISTS guest_visit_request (
    request_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id          INTEGER NOT NULL REFERENCES student(student_id),
    guest_id            INTEGER NOT NULL REFERENCES guest_profile(guest_id),
    visit_date          TEXT NOT NULL,
    valid_from          TEXT NOT NULL,
    valid_to            TEXT NOT NULL,
    purpose             TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'Pending' CHECK (status IN ('Pending', 'Approved', 'Rejected')),
    decided_by_warden_id INTEGER REFERENCES warden(warden_id),
    decided_at          TEXT,
    max_entries         INTEGER NOT NULL DEFAULT 1,
    entries_used        INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_visit_status ON guest_visit_request(status, visit_date);
CREATE INDEX IF NOT EXISTS idx_visit_guest ON guest_visit_request(guest_id);
CREATE INDEX IF NOT EXISTS idx_guest_host ON guest_profile(host_student_id);

-- One row for every verification attempt. Never edited or deleted by the application.
CREATE TABLE IF NOT EXISTS entry_log (
    entry_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_type     TEXT CHECK (subject_type IN ('student', 'guest')),  -- NULL = unidentified person
    student_id       INTEGER REFERENCES student(student_id),
    guest_id         INTEGER REFERENCES guest_profile(guest_id),
    visit_request_id INTEGER REFERENCES guest_visit_request(request_id),
    attempted_at     TEXT NOT NULL,
    method           TEXT NOT NULL DEFAULT 'None' CHECK (method IN ('Face', 'Eye-Region', 'None')),
    result           TEXT NOT NULL CHECK (result IN ('Granted', 'Denied')),
    score            REAL,
    failure_reason   TEXT,
    door_status      TEXT NOT NULL CHECK (door_status IN ('Unlocked', 'Locked')),
    needs_review     INTEGER NOT NULL DEFAULT 0,
    failure_code     TEXT,                 -- stable code for programs (e.g. liveness_failed); failure_reason is for people
    liveness         TEXT                  -- passed / failed / not_checked
);
CREATE INDEX IF NOT EXISTS idx_entry_time ON entry_log(attempted_at);
CREATE INDEX IF NOT EXISTS idx_entry_student ON entry_log(student_id, attempted_at);

-- The entry log is an audit trail (SRS NFR-07): the database itself refuses to change or remove a row.
CREATE TRIGGER IF NOT EXISTS entry_log_no_update BEFORE UPDATE ON entry_log
BEGIN SELECT RAISE(ABORT, 'entry_log rows cannot be changed'); END;
CREATE TRIGGER IF NOT EXISTS entry_log_no_delete BEFORE DELETE ON entry_log
BEGIN SELECT RAISE(ABORT, 'entry_log rows cannot be deleted'); END;

CREATE TABLE IF NOT EXISTS attendance (
    attendance_id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id    INTEGER NOT NULL REFERENCES student(student_id),
    local_date    TEXT NOT NULL,
    local_time    TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('Present', 'Late')),
    UNIQUE (student_id, local_date)   -- prevents duplicate daily attendance
);

-- Manual exit recording by staff.
CREATE TABLE IF NOT EXISTS exit_record (
    exit_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_type TEXT NOT NULL CHECK (subject_type IN ('student', 'guest')),
    subject_id   INTEGER NOT NULL,
    recorded_by_role TEXT NOT NULL,
    recorded_by_id   INTEGER NOT NULL,
    recorded_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alert (
    alert_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER REFERENCES student(student_id),   -- NULL = unknown person
    alert_type TEXT NOT NULL CHECK (alert_type IN ('Emergency', 'Unauthorized Attempt')),
    message    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'New' CHECK (status IN ('New', 'Acknowledged')),
    acknowledged_by_warden_id INTEGER REFERENCES warden(warden_id),
    acknowledged_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_alert_status ON alert(status, created_at);

CREATE TABLE IF NOT EXISTS emergency_contact (
    contact_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_name TEXT NOT NULL,
    contact_type TEXT NOT NULL CHECK (contact_type IN ('Security', 'Police', 'Other')),
    phone        TEXT NOT NULL,
    email        TEXT NOT NULL,
    admin_id     INTEGER REFERENCES admin(admin_id)
);

CREATE TABLE IF NOT EXISTS report (
    report_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id            INTEGER NOT NULL REFERENCES student(student_id),
    period_start          TEXT NOT NULL,
    period_end            TEXT NOT NULL,
    days_present          INTEGER NOT NULL,
    late_entries          INTEGER NOT NULL,
    attendance_percentage REAL NOT NULL,
    email_status          TEXT NOT NULL DEFAULT 'Pending' CHECK (email_status IN ('Pending', 'Sent', 'Failed')),
    generated_date        TEXT NOT NULL
);

-- Small support tables
CREATE TABLE IF NOT EXISTS setting (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS login_attempt (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    email      TEXT NOT NULL,
    attempted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_attempt ON login_attempt(email, attempted_at);

-- Emails waiting to be sent (or already sent / failed). Alerts and reports are saved first; mail is delivered afterwards,
-- so a mail problem can never lose an alert, and failed messages can be retried.
CREATE TABLE IF NOT EXISTS email_outbox (
    email_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,                         -- emergency / security / weekly_report / test
    recipient  TEXT NOT NULL,
    subject    TEXT NOT NULL,
    body       TEXT NOT NULL,
    related_id INTEGER,                               -- alert_id or report_id
    status     TEXT NOT NULL DEFAULT 'Pending' CHECK (status IN ('Pending', 'Sent', 'Failed', 'Skipped')),
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    sent_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_status ON email_outbox(status, created_at);

-- One weekly report per student per period, even if the job runs twice.
CREATE UNIQUE INDEX IF NOT EXISTS idx_report_unique ON report(student_id, period_start, period_end);
