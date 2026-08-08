@echo off
rem Weekly judge-drift canary (tests/validation/canary.py): re-evaluates the frozen
rem sentinel set and alerts on drift vs the stored baseline. Exit 2 = drift alert —
rem Task Scheduler surfaces it as "Last Run Result" 0x2; details go to the log below.
rem Register (weekly, Monday 08:30, adjust freely):
rem   schtasks /Create /TN "jobsearch-canary" /SC WEEKLY /D MON /ST 08:30 ^
rem     /TR "D:\Github\jobsearch_pipeline\run_canary.bat"
cd /d %~dp0
if not exist logs mkdir logs
".venv\Scripts\python.exe" tests\validation\canary.py >> logs\canary.log 2>&1
exit /b %errorlevel%
