# Manual test checklist

Do this on your own computer after setup ([README.md](../README.md)). Tick each line. "Expected" says what you should see.
Use a **fresh database** for a clean run (stop the app, delete `instance/hostel.db`, start again, create the admin).

Use Edge or Chrome at `http://127.0.0.1:5000`.

## 1. Login and roles
- [ ] Wrong password -> a generic "Incorrect email or password." message (it never says which part was wrong)
- [ ] 5 wrong passwords for one email -> "Too many failed attempts"; a different email still works
- [ ] Admin logs in -> Admin dashboard. Log out -> back to the login page
- [ ] Logged out, open `/admin/` -> sent to the login page
- [ ] Log in as a student, open `/students/` -> "Access denied" page (403)
- [ ] Password fields have a show/hide toggle

## 2. Admin: people
- [ ] Add a warden; add 2 students (one assigned to the warden)
- [ ] Empty form -> red messages under the fields, nothing saved
- [ ] Duplicate email or registration number -> clear error
- [ ] Search students by name / registration number / room
- [ ] Deactivate a student -> asks for confirmation; that student can no longer log in; Activate brings them back

## 3. Face enrollment (Admin -> Enrollment)
- [ ] Open a student: the camera starts by itself (allow it when asked)
- [ ] "Save" is disabled until the consent box is ticked
- [ ] Capture 5 photos -> each is checked (dark/blurry/small are rejected with a reason)
- [ ] Save -> status becomes Enrolled. No photo is stored anywhere in the project folder
- [ ] Remove the enrollment -> status becomes Not enrolled

## 4. Entry terminal (`/terminal/`, on this computer)
- [ ] Enrolled student looks at the camera and blinks -> "Access granted" and the name
- [ ] Same student holds a printed photo/phone photo without blinking -> denied (liveness)
- [ ] An un-enrolled person -> denied "Face not recognised"
- [ ] 3 failed attempts within 5 minutes -> a security alert appears (Alerts page and menu badge)
- [ ] Granted and denied attempts show in Logs; Logs cannot be edited or deleted

## 5. Attendance and exits
- [ ] A granted entry before the curfew -> Present; after the curfew -> Late (Attendance page; change the curfew in Settings to test)
- [ ] A student with no entry that day shows as having no record (absent) once the day is over
- [ ] Warden/Admin records an exit for a student; the student can then enter again
- [ ] Student sees only their own attendance

## 6. Guests
- [ ] Student requests a guest visit (guest name, host, time window, purpose)
- [ ] Warden approves it (and can choose the number of allowed entries, one-time by default); rejecting asks for confirmation
- [ ] Warden enrolls the guest's face; the guest is granted entry inside the window and only up to the allowed entries
- [ ] Outside the window or after the entries are used -> denied

## 7. Alerts and email
- [ ] Student presses the emergency button -> warden sees an Emergency alert; pressing again within 2 minutes does not create a duplicate
- [ ] Warden acknowledges an alert -> status changes; students cannot acknowledge
- [ ] Admin -> Emergency contacts: add, edit, delete (with confirmation)
- [ ] Admin -> Email: without SMTP it says "Not configured"; messages stay Pending/Skipped and nothing crashes
- [ ] With SMTP in `.env`: "Send test" delivers a message; the password is never shown on the page

## 8. Reports
- [ ] Reports -> Daily, Weekly, Monthly each open; tabs Attendance / Security / Parent emails work
- [ ] Search and sort change the table; an unfinished period explains that only past days are counted
- [ ] A period in the future says "has not started"
- [ ] Print (Ctrl+P) shows a clean report without the menu and buttons
- [ ] `flask --app run send-weekly-reports` creates one report per student; running it again creates none (no duplicates)

## 9. Screens
- [ ] Resize the browser to phone width (about 390 px), tablet (768) and desktop: no sideways page scroll; the menu opens with the hamburger button
- [ ] Empty lists show a friendly message, not a blank page

## 10. Secrets
- [ ] `git status` does not list `.env`, `instance/` or any `.db` file
- [ ] `.env.example` contains no real passwords or keys
