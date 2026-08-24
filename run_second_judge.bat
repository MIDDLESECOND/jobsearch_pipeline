@echo off
rem Deepdive doorbell slot. Schedule this AFTER each run_pipeline.bat slot (e.g. +15 min).
rem
rem This .bat used to run the paid second-opinion layer (pipeline.py second-judge) and
rem then the doorbell. The judge was RETIRED 2026-08-22 - see CHANGELOG - but the file
rem keeps its name on purpose: the Windows Task Scheduler action points at this path, so
rem renaming it would need a scheduler edit and the doorbell is what the slot is really
rem for. Killing the scheduled task instead of editing this file would have taken the
rem DOORBELL down with the judge, and the doorbell is the only trigger deepdive batches
rem have.
rem
rem To restore the judge: put the two commented lines back. second_judge.py, the
rem second_opinions table, its 728 collected opinions, and the `pipeline.py second-judge`
rem CLI are all intact - nothing was deleted, only unscheduled.
cd /d %~dp0
rem ".venv\Scripts\python.exe" pipeline.py second-judge
rem set JUDGE_RC=%errorlevel%
rem Doorbell: counts the deepdive-actionable zone and pops the batch proposal (zone count
rem + time/quota estimate). Read-only helper. PYTHONUTF8 guards its Chinese popup text
rem against the GBK console codepage. It tees into the day log via core.run_log, so which
rem branch it took - popup, no-batchable-row, quota read failure, traceback - is
rem recoverable afterwards; under Task Scheduler the console output goes nowhere.
rem Its exit code IS this slot's outcome now that it is the only command here: a doorbell
rem that cannot run means no batch is ever proposed, which is exactly the silent-death
rem class the health sentinels exist to catch.
set PYTHONUTF8=1
".venv\Scripts\python.exe" notify_deepdive_batch.py
exit /b %errorlevel%
