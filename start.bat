@echo off
rem Starts the Hostel Security System. Double-click this file. Keep this window OPEN while using the site.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo The .venv folder was not found in %cd%
  echo Follow the setup steps in README.md first.
  pause
  exit /b 1
)
echo Starting... when you see "Running on http://127.0.0.1:5000", open that address in Edge or Chrome.
echo Do NOT close this window (closing it stops the site). Press Ctrl+C to stop.
echo.
".venv\Scripts\python.exe" run.py
echo.
echo The app stopped. If there is an error above, copy it and send it for help.
pause
