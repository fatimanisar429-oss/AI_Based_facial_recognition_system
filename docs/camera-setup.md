# Camera setup

The system uses the computer's own camera through the browser (no extra hardware).

## Rules the browser enforces
- The camera works only on a **secure origin**: `http://127.0.0.1:5000` or `http://localhost:5000` on the same computer, or HTTPS.
  Opening `http://192.168.x.x:5000` from another machine will **not** give camera access. This is a browser rule, not a bug.
- The first time, the browser asks "Allow camera?". Choose **Allow**. If you blocked it by mistake: click the camera/lock icon
  in the address bar, set Camera to Allow, then reload the page.
- Only one program can use the camera at a time. Close Zoom/Teams/other tabs that use it.
- The site sends `Permissions-Policy: camera=(self)`, so embedded pages can never use it.

## Entry terminal (`/terminal/`)
Open it **on the gate computer**. Other machines get "Access denied" on purpose, because the terminal needs no login.
To allow a specific gate computer on the network, add its IP address to `TERMINAL_ADDRESSES` in `.env`.

## Getting good recognition
- Face the camera at eye level, about 40-80 cm away, with even light in front of you (not a window behind you).
- Enroll the way you will normally appear at the gate (with or without glasses).
- Enrollment asks for 5 photos; the checker rejects dark, blurry, tiny or inconsistent ones and says why.
- At the terminal, look at the camera and **blink once or twice** normally. The blink check takes about 3 seconds.
- Photos are analysed in memory and **not stored**; only an encrypted 128-number template is saved
  ([biometric-data-lifecycle.md](biometric-data-lifecycle.md)).

## Troubleshooting
| Symptom | Fix |
|---|---|
| "Camera blocked" | Allow the camera for this site (address bar icon), reload |
| Black picture / "camera in use" | Close other programs/tabs using the camera |
| "No face found" | Move closer, add light, remove a cap/mask, look straight at the camera |
| "Liveness failed" | Blink normally while looking at the camera; do not hold up a photo or a phone screen |
| "Face not recognised" | Re-enroll in the lighting you will really use |
| Cannot open `/terminal/` | Open it on the gate computer, or add its address to `TERMINAL_ADDRESSES` |
