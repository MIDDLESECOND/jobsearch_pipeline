@echo off
rem Daily integrity check + dated evidence backup.
rem Added 2026-08-18 as a watchdog while the live DB sat on a USB path; repurposed
rem 2026-08-19 after the repo moved to internal storage.
rem
rem The backup deliberately lands on a DIFFERENT physical device than the live
rem database - a backup sharing a disk with its original is not a backup. Set
rem JOBSEARCH_BACKUP_DIR to that device's folder (setx, same convention as the API
rem keys). With it unset this falls back to the in-repo gitignored backups\ folder,
rem which still works but gives up the separate-device property.
rem
rem No blocking dialogs: this runs unattended, and a MessageBox nobody clicks hangs
rem the task forever (measured on the deepdive doorbell, 2026-08-17). Failures go to
rem the log and to a non-zero exit code, which Task Scheduler shows as Last Run Result.
rem House rule: this file must stay pure ASCII (cmd.exe parses by byte offset).
setlocal enabledelayedexpansion
cd /d %~dp0
set REPO=%~dp0
set PY=%REPO%.venv\Scripts\python.exe
if "%JOBSEARCH_BACKUP_DIR%"=="" (set DEST=%REPO%backups) else (set DEST=%JOBSEARCH_BACKUP_DIR%)
set LOG=%DEST%\drive_watch.log

rem The destination may be a removable drive. If it is not mounted, say so and stop -
rem do not fail into a dialog, and do not silently write the backup next to the live DB.
if not exist "%DEST%\" mkdir "%DEST%" 2>nul
if not exist "%DEST%\" (
  echo [%date% %time%] backup destination unavailable: %DEST%
  exit /b 3
)

echo [%date% %time%] drive watch start >> "%LOG%"

rem 1) integrity check, read-only
%PY% -c "import sqlite3,os,sys;p=os.path.join(os.getcwd(),'jobs.db');c=sqlite3.connect('file:'+p.replace(os.sep,'/')+'?mode=ro',uri=True);r=c.execute('PRAGMA quick_check').fetchone()[0];print('quick_check:',r);sys.exit(0 if r=='ok' else 1)" >> "%LOG%" 2>&1
if errorlevel 1 goto :fail

rem 2) dated backup, skip if today's already exists (backup never overwrites)
for /f %%i in ('%PY% -c "import datetime;print(datetime.date.today().isoformat())"') do set STAMP=%%i
set ZIP=%DEST%\jobsearch_evidence_backup_!STAMP!.zip
if exist "!ZIP!" (
  echo backup exists for !STAMP!, skipping >> "%LOG%"
) else (
  %PY% "%REPO%pipeline.py" backup --output "!ZIP!" >> "%LOG%" 2>&1
  if errorlevel 1 goto :fail
)

rem 3) keep only the newest 14 (raised from 2 on 2026-08-19: a corruption noticed on
rem    day 3 had no clean copy under the old retention, and 14 packs are ~1.4 GB)
for /f "skip=14 delims=" %%f in ('dir /b /o-d "%DEST%\jobsearch_evidence_backup_*.zip" 2^>nul') do (
  del "%DEST%\%%f"
  echo pruned %%f >> "%LOG%"
)

echo [%date% %time%] drive watch ok >> "%LOG%"
exit /b 0

:fail
echo [%date% %time%] DRIVE WATCH FAILED - see %LOG% >> "%LOG%"
exit /b 1
