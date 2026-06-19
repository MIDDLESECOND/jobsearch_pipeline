@echo off
cd /d %~dp0
if not exist logs mkdir logs
echo ===== run started %DATE% %TIME% ===== >> logs\pipeline.log
".venv\Scripts\python.exe" pipeline.py run >> logs\pipeline.log 2>&1
echo ===== run ended   %DATE% %TIME% ===== >> logs\pipeline.log
