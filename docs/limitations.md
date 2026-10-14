# Limitations (please read before relying on the system)

This is a student project. It demonstrates the idea; it is **not** a certified or production-grade security product.

## Biometrics
- **The blink check is not strong anti-spoofing.** It stops a still photo held up to the camera. It does not reliably stop a
  video of the person, a mask, or a deliberate attack.
- **"Iris recognition" is not real iris recognition.** A browser webcam cannot capture iris texture. The optional eye-region
  fallback compares the eye area only, could not reliably separate different people in our measurements, and is therefore
  **off by default** ([recognition-limits.md](recognition-limits.md)).
- Face matching can fail in poor light, with masks or heavy occlusion, with big pose changes, and for twins or close relatives.
  When two people are too similar the system denies the entry rather than guessing.
- Accuracy was measured on a small set of images, not a large population. Do not claim a percentage for real use.

## System
- **No door hardware.** "Unlocked/Locked" is a screen status and a log entry only.
- Single hostel; one SQLite file; meant for one server computer, not many simultaneous gates.
- The entry terminal needs no login and is limited by IP address to the gate computer. It does not replace a locked door.
- Email needs a real SMTP account. Without it, alerts and reports are still saved and shown; the emails stay Pending/Skipped.
- The emergency alert notifies the warden and the emergency contacts **you** configure. It does **not** contact the police
  or any outside service by itself.
- Attendance: "Absent" means no entry record for that hostel day (the day starts at 04:00 local time). Leave, sickness and
  visits outside the system are not modelled.
- Sessions expire after 15 minutes idle; 5 wrong passwords lock an email address for 15 minutes.
- The weekly parent email is sent by a background scheduler that runs only while the app is running.
- Not tested with real hostel data, with many users at once, or with phones used as the terminal.
- HTTPS is not set up by the project. Beyond a local demo, serve it behind HTTPS and set `SESSION_COOKIE_SECURE=1`.

## Privacy
Student data and encrypted face templates are personal data. Get consent, restrict who can use the admin account,
back up `BIOMETRIC_KEY`, and delete a student's template when they leave (Admin, Enrollment, Remove).
