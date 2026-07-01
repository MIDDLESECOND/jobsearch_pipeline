@echo off
rem The app itself tees stdout+stderr into logs\pipeline.log (core.run_log) with the
rem session markers, so this no longer redirects — doing both would double-log every run.
cd /d %~dp0
".venv\Scripts\python.exe" pipeline.py run
exit /b %errorlevel%
