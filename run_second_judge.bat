@echo off
rem Second-opinion layer (second_judge.py): submits the day's interesting zone to the
rem Anthropic Batch API, waits for the batch (usually <1h, --wait 90 cap), ingests the
rem opinions, and rebuilds exactly the daily reports whose rows gained one (none of
rem them on a slot where nothing landed).
rem Schedule this AFTER each run_pipeline.bat slot (e.g. +15 min)  -  it only spends on
rem rows the main run already evaluated, and a crash here never blocks the pipeline.
rem The app tees stdout/stderr into logs\second-judge-*.log via core.run_log.
cd /d %~dp0
".venv\Scripts\python.exe" pipeline.py second-judge
set JUDGE_RC=%errorlevel%
rem Doorbell: now that opinions are in, pop the deepdive batch proposal (zone count +
rem time/quota estimate). Read-only helper; its outcome never masks the judge exit code.
rem PYTHONUTF8 guards the doorbell's Chinese popup text against the GBK console codepage.
set PYTHONUTF8=1
".venv\Scripts\python.exe" notify_deepdive_batch.py
exit /b %JUDGE_RC%
