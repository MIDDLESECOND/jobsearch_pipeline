@echo off
rem Reminder popup for the corpus-mode check-ins (CHANGELOG 2026-09-09). Usage:
rem   run_reminder.bat bayarea      one-shot check-in after the move
rem   run_reminder.bat quarterly    recurring demand-analysis nudge
rem   run_reminder.bat "any text"   free-form message
rem The two keywords exist because schtasks caps /TR at 261 characters, so the message
rem text has to live here rather than in the task definition. Registered with
rem StartWhenAvailable, so a slot missed while the machine was off fires at the next
rem boot instead of never. Pure ASCII, CRLF - see .gitattributes. The message travels
rem through an environment variable so cmd quoting and PowerShell quoting never meet.
set "REMINDER_MSG=%~1"
if /I "%~1"=="bayarea" set "REMINDER_MSG=Corpus-mode check-in (jobsearch_pipeline): about a month in the Bay Area now. 1) Re-point the config.yaml searches to Bay Area + remote. 2) Check the 2026-09-11 role-family retarget is capturing: the ai_ml_engineering search must show rows in python pipeline.py stats, else its query syntax is wrong (fix here, not the decision). 3) Decide whether Adzuna comes back (dropped 2026-09-09, see CHANGELOG). 4) Confirm the corpus is still growing: python pipeline.py stats"
if /I "%~1"=="quarterly" set "REMINDER_MSG=Quarterly market read (jobsearch_pipeline): the fetch-only corpus has another 3 months of JDs in jobs.db. Run the demand analysis - skills frequency, titles, salary bands, which employers. If the analysis script does not exist yet, build it now: a corpus nobody reads is a pile."
powershell -NoProfile -ExecutionPolicy Bypass -Command "Add-Type -AssemblyName System.Windows.Forms; [void][System.Windows.Forms.MessageBox]::Show($env:REMINDER_MSG, 'jobsearch_pipeline reminder', 'OK', 'Information', 'Button1', 'ServiceNotification')"
exit /b %errorlevel%
