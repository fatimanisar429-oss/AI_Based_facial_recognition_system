# Biometric data lifecycle

This page explains what biometric data the system keeps, where, who can reach it, and how it is changed or
deleted. It describes the system as built so far (enrollment, Phase 6). Verification is added in Phase 7.

## What is stored

| Item | Stored? | Notes |
|---|---|---|
| Photographs / video frames | **No** | Pictures live only in the browser's memory while the admin captures them. The server reads them, extracts numbers and discards them. Nothing is written to disk. |
| Face template | Yes | 128 numbers describing a face (`face_recognition` / dlib encoding), one row per sample (5 per student). |
| Eye-region template | Yes | 80 numbers (Local Binary Pattern histograms of the skin around both eyes), one row per sample. This is **not** iris recognition. |
| Quality numbers (brightness, sharpness) | No | Shown to the admin during capture only. |

Table: `biometric_profile` (`owner_type`, `owner_id`, `kind` = `face`/`eye`, `sample_no`, `template`, `created_at`).

## How templates are protected

* Each template is packed as one version byte plus float32 numbers and **encrypted with Fernet** (AES-128 + HMAC)
  using `BIOMETRIC_KEY` from the `.env` file before it is written to SQLite.
* A copy of the database file alone is useless without the key. The key is **not** in the database or in Git.
* **Back the key up.** If it is lost, every enrolled template becomes unreadable and all students must be enrolled
  again. If it leaks together with the database, templates can be decrypted. Treat it like a password.
* Templates are never included in pages or JSON responses, and are never written to logs.

## Who can do what

| Action | Admin | Warden | Student |
|---|---|---|---|
| Enroll / re-enroll a face | Yes | No | No |
| Remove an enrollment | Yes | No | No |
| See whether a student is enrolled (yes/no, sample count, date) | Yes | Yes | No |
| See template values | Nobody (they are only used by the recognition code) | | |

Every enrollment and removal writes a log line with the student id and admin id (no biometric content).

## Lifecycle

1. **Consent.** The admin confirms the student is present and agrees (a required checkbox; the server refuses to
   save without it). The checkbox is a gate, not a record: the system does not store the consent itself.
2. **Enrollment.** Five pictures are checked one by one (face found, one face only, bright and sharp enough, big
   enough). On Save, all five are analysed, must look like the same person, and must not match a different
   enrolled student. Templates are saved in one transaction.
3. **Use.** Phase 7 loads the templates of *active* students only to compare them with the live face at the entry
   terminal.
4. **Update.** Re-enrolling replaces all old templates in one transaction. If anything fails, the old enrollment
   stays untouched.
5. **Removal.** The admin can delete a student's templates at any time (with a confirmation).
6. **Deactivation.** Deactivating a student keeps their templates (so reactivation works) but they are excluded
   from matching. Remove the enrollment explicitly if the data should not be kept.
7. **Backups.** A copy of the database contains the encrypted templates. Protect backups like the live database.

## Limits to be aware of

* Checks use general thresholds (`.env` / `config.py`: `BRIGHTNESS_MIN`, `SHARPNESS_MIN`, `FACE_MIN_PX`,
  `FACE_THRESHOLD`). They must be tuned with the real webcam, and five good samples do not guarantee a reliable template.
* Two people who look alike (for example twins) may be refused as "already enrolled".
* A blink check (Phase 7) is not strong protection against photos or replayed video. This is a university project,
  not production-grade biometric security.

## Guests (Phase 9)

A guest is someone visiting a student. The same kind of template is stored (`owner_type = 'guest'`), encrypted the same way.

| Question | Answer |
|---|---|
| Who registers a guest's face? | A **warden** (or the admin) at the gate, with the same camera page and the same quality checks as for students. |
| When? | Only for a visit that is **approved**. Not for pending or rejected requests. |
| Consent | The same required checkbox. |
| Can a guest become a student, or the reverse? | No. A face that matches an enrolled student cannot be registered as a guest, and a face held by a guest cannot be enrolled as a student. |
| Returning guests | If the new face matches a guest we already know, that profile is **reused** (the match is the verification). Nothing new is stored. |
| Who is matched at the terminal? | Only guests who have at least one **approved** visit. Everyone else is simply unknown. |
| How long is it kept? | Until a warden or admin presses **Delete face data** on the visit page. It is **not** deleted automatically after the visit, so a returning guest does not have to register again. Delete it when the guest should no longer be recognised. |
| What is logged? | Entries and refusals reference the guest and the visit, never any biometric values. |

A face recognised as a guest is only a *candidate*. The server then checks that the visit is approved, inside its time window, has an
entry left, and that the host student is still active. Spending the entry is one atomic database update, so the same entry cannot be
used twice. A guest never creates student attendance. Exits are recorded manually by staff.
