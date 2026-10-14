# Security review (Phase 12)

What is protected, where, and how it was checked. Automated checks live in
[tests/test_security_review.py](../tests/test_security_review.py) (run: `python -m pytest tests/test_security_review.py -q`).

| Area | What the system does | Checked by |
|---|---|---|
| Passwords | Stored only as Werkzeug hashes; never shown in pages or kept in the session | `test_passwords_are_hashed_and_never_in_pages_or_session` |
| Login | Generic error message, 5 failed attempts per email lock it for 15 minutes, session expires after 15 idle minutes | `tests/test_auth.py` |
| Roles (RBAC) | Every route checks the role **on the server**. Wrong role gets 403, logged-out visitors go to the login page | `test_every_route_has_a_security_policy`, `test_anonymous_visitors_are_sent_to_login`, `test_wrong_role_gets_403_and_right_role_does_not` (every route x every role) |
| CSRF | Every POST needs the session's token (form field or `X-CSRF-Token` header); tokens from another session are rejected | `test_every_post_without_a_csrf_token_is_rejected`, `test_a_token_from_another_session_is_rejected` |
| Entry terminal | No login, so it answers only the addresses in `TERMINAL_ADDRESSES` (default: the same computer) | `test_the_entry_terminal_only_answers_the_gate_computer` |
| SQL injection | All user values are bound parameters. The few f-string queries splice in only fixed table/column names or `?` placeholders; a test scans the code for any other use | `test_sql_is_never_built_from_user_input`, injection tests in `tests/test_reports.py`, `tests/test_people.py` |
| XSS | Jinja auto-escapes every value; tests submit script tags as names/search text | `tests/test_reports.py::test_report_pages_are_safe`, `tests/test_people.py` |
| Biometric data | Photos are never stored; only a Fernet-encrypted template. Key comes from `BIOMETRIC_KEY` (never in the repo) | `tests/test_biometrics.py`, [biometric-data-lifecycle.md](biometric-data-lifecycle.md) |
| Audit trail | `entry_log` is append-only: the database refuses UPDATE and DELETE | `tests/test_phase8_pages.py` |
| Denied attempts | A denied attempt is never attributed to a student, so a stranger cannot mark a student present or frame them | `tests/test_attendance.py`, `tests/test_alerts_logic.py`, `tests/test_guests_logic.py` |
| Secrets in git | `.env`, `instance/`, `*.db`, `.venv/` are ignored; `.env.example` has only placeholders; no database is tracked | `test_secrets_and_data_files_are_git_ignored`, `test_env_example_holds_only_placeholders` |
| Dev quick login | Off by default, works only from the same computer, 404 otherwise | `test_dev_quick_login_is_off_by_default`, `tests/test_dev_login.py` |
| Errors | Friendly pages, no stack traces, file paths or SQL text | `test_error_pages_do_not_leak_details` |
| Headers / cookies | `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy`, camera allowed for this site only, `no-store` for logged-in pages; session cookie HttpOnly + SameSite=Lax | `test_every_page_sends_the_security_headers`, `test_session_cookie_is_http_only_and_same_site` |
| Email credentials | Read from `.env`, never displayed or logged; placeholders count as "not configured" | `tests/test_alerts_pages.py`, `tests/test_alerts_logic.py` |

## Checks that were deliberately broken to prove the tests work (mutation checks)
Loosening a role on one route, adding `student` to the acknowledge route, switching CSRF off, removing the terminal address check,
removing the role check, weakening `X-Frame-Options`, and building one SQL string from a value: each made at least one test fail.

## Known gaps (not fixed, by design or scope)
- No HTTPS inside the project; use a reverse proxy/HTTPS for any real deployment and set `SESSION_COOKIE_SECURE=1`.
- The Flask development server is used. For real use run behind a production server (for example waitress).
- No rate limit on the terminal endpoint beyond the failed-attempt alerts.
- The blink check is not strong anti-spoofing ([limitations.md](limitations.md)).
- No two-factor login; admin accounts need a strong password and a physically safe computer.
