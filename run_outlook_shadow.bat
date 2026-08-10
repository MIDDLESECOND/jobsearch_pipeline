@echo off
rem Read-only Outlook job-alert shadow scan. First run `pipeline.py email-shadow --login`
rem interactively; scheduled runs deliberately never open a login prompt.
cd /d %~dp0
".venv\Scripts\python.exe" pipeline.py email-shadow
exit /b %errorlevel%
