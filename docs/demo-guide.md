# FYP demo guide (about 15 minutes)

## Before you present (10 minutes earlier)
1. Fresh database: stop the app, delete `instance/hostel.db`, then `flask --app run create-demo-users`.
2. In `.env` set `DEV_QUICK_LOGIN=1` (local demo only), then `python run.py`.
3. Open `http://127.0.0.1:5000` in Edge/Chrome on the computer with the camera. Allow the camera once.
4. Close other programs that use the camera. Sit facing a light, not a window.
5. Have a second person (or a printed photo) ready for the "unknown person" and "photo attack" steps.

## Script
1. **The idea (1 min).** Software-only hostel entry: browser camera, face recognition, blink check, attendance, alerts, reports.
   Say clearly: no door hardware, door status is simulated.
2. **Admin (2 min).** Click *Admin* on the login page. Show the dashboard, add a student, open Students and search.
3. **Enrollment (2 min).** Click *Register a face* (or Enrollment -> the student). The camera starts by itself. Tick consent,
   capture 5 photos, point out the quality messages, save. Mention: photos are not stored, only an encrypted template.
4. **Entry terminal (3 min).** Open `/terminal/`. Look at the camera and blink -> *Access granted*. Show Logs and Attendance
   (Present / Late depends on the curfew in Settings).
5. **Security cases (2 min).** (a) the unknown person -> denied; (b) a photo of the student on a phone -> denied by the blink
   check; (c) three failed tries -> a security alert appears with a badge in the menu.
6. **Guest (2 min).** As Student: request a guest visit. As Warden: approve it, enroll the guest's face. Show guest entry
   works inside the window and only for the allowed number of entries.
7. **Emergency and alerts (1 min).** Student presses the emergency button; Warden sees and acknowledges it.
8. **Reports (2 min).** Reports -> Weekly, Security tab, Parent emails tab; use search/sort; Print preview.
9. **Honesty slide (1 min).** Show [limitations.md](limitations.md): blink check is not strong anti-spoofing, the eye-region
   feature is not true iris recognition, not production-grade security.

## Questions you may be asked
- *Why is it called iris recognition?* The project name follows the SRS. A webcam cannot capture iris texture; the eye-region
  matcher is experimental and off by default ([recognition-limits.md](recognition-limits.md)). Face recognition is what decides entry.
- *Can a photo or video fool it?* A still photo is stopped by the blink check; a video or mask may not be. Said openly in the limitations.
- *Where is the face data?* Only as an encrypted 128-number template in the database; the key is in `.env` (never in git).
- *What if the camera is blocked?* [camera-setup.md](camera-setup.md).
- *What would production need?* Real door hardware, HTTPS and a production server, stronger liveness, larger accuracy tests, privacy policy.

## After the demo
Set `DEV_QUICK_LOGIN=0` again. Do not commit `.env`, `instance/` or any `.db` file.
