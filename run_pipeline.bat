@echo off
rem The app itself tees stdout+stderr into the day's logs\pipeline-YYYY-MM-DD.log (core.run_log)
rem with the session markers, so this no longer redirects — doing both would double-log every run.
cd /d %~dp0
".venv\Scripts\python.exe" pipeline.py run
exit /b %errorlevel%
