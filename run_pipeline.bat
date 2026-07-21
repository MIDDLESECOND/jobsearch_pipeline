@echo off
rem The app itself tees stdout+stderr into the day's logs\pipeline-YYYY-MM-DD.log (core.run_log)
rem with the session markers, so this no longer redirects — doing both would double-log every run.
cd /d %~dp0
rem --scheduled: honor the cooldown skip (no-op if the last successful run ended < 1h ago).
rem Manual runs (`python pipeline.py run` in a terminal) deliberately omit it.
".venv\Scripts\python.exe" pipeline.py run --scheduled
exit /b %errorlevel%
