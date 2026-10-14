# Alerts and email

What the system does when something needs the warden's attention, and exactly what it does **not** do.

## Emergency alerts (student button)

* The student presses **Emergency** on the dashboard or the *Emergency alert* page and must **confirm** in a dialog.
* The alert is **saved first** (student, time, status *New*), shown on the warden dashboard and in the Alerts list, and emailed to
  **all active wardens and all emergency contacts** (if a mail server is configured).
* The alert is always for the **logged-in student**; nothing in the browser request can name another student.
* Pressing again within 2 minutes does not create a second alert (it tells the student the warden already has it).
* A warden **acknowledges** an alert (who and when are recorded). Only wardens can; admins can view.
* It does **not** call the police, an ambulance or any other service. The pages and emails say so. Emergency contacts are
  people who receive an email, nothing more.

## Automatic security alert (repeated failures)

An *Unauthorized Attempt* alert is raised when **3 counted failures happen within 5 minutes** (`ALERT_FAILED_THRESHOLD`,
`ALERT_WINDOW_MINUTES`).

**Policy (deliberate and tested):**

| Counted | Not counted |
|---|---|
| Blink check failed, face not recognised, two identities matched equally (a person was in front of the camera and could not be verified) | No face, too dark/bright, blurry, too far, unusable frames (technical problems), nobody enrolled, processing errors, a *recognised guest* refused for visit rules (expired, used...) |

* The people are **unidentified**, so failures are counted for the **terminal as a whole** and the alert names **no student**.
* A successful entry in between does **not** reset the count.
* **One alert per incident:** after an alert, no new one is raised for the next 5 minutes, even if failures continue. A new alert
  needs a new burst after that.
* The log row and its alert are saved in the same database transaction.
* Recipients: all active wardens plus contacts of type **Security** (not Police/Other).

## How email is sent

1. The alert or report is saved. 2. Its emails are added to the `email_outbox` table. 3. A background thread (and a job every
5 minutes) delivers them. Status per message: **Sent**, **Failed** (retried, up to 5 attempts), **Skipped** (no mail server).

* A slow or broken mail server never delays a page and can never lose an alert.
* Without a mail server the whole system works; emails are marked *Skipped* and can be retried later (Admin → Email).
* Credentials (`MAIL_PASSWORD`, `MAIL_USERNAME`) are never shown, logged or stored in error text.
* **Check your settings:** Admin → **Email** → *Send test*, or `flask --app run send-test-email --to you@example.com`.

`.env` settings: `MAIL_SERVER`, `MAIL_PORT`, `MAIL_USE_TLS`, `MAIL_USERNAME`, `MAIL_PASSWORD`, `MAIL_DEFAULT_SENDER`.
For Gmail use an *app password*, port 587, TLS on.

## Weekly parent report

* Every **Sunday 20:00** (local time; `WEEKLY_REPORT_DAY`, `WEEKLY_REPORT_TIME`) a report is created for each **active** student
  covering the **7 complete hostel days before today**, and emailed to the student's parent email.
* Contents: days present, late entries, days absent, attendance percentage (days present / 7).
* The report row is saved even if the email fails (`Pending` / `Sent` / `Failed`). The next weekly run re-queues last
  period's failed or skipped emails.
* A student's report for a period is created **once**, even if the job runs twice (a unique database index).
* Run it by hand: `flask --app run send-weekly-reports`.
* The scheduler starts once per process; never in tests, never in Flask's debug-reloader parent process, and not when
  `SCHEDULER_ENABLED=0`. If the computer is off at the scheduled time, the report is not created until the next run.

## Limits

* Email depends on a configured mail server and an internet connection. Delivery to the inbox is not guaranteed
  (spam filters).
* The wording "unidentified people" is intentional: a face that fails to match is not proof of who someone is.
* Alerts appear in the web pages; there is no sound, SMS or push notification.