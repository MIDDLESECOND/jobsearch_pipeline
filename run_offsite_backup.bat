@echo off
rem Offsite copy of the newest verified evidence pack, encrypted before it leaves
rem this machine. Added 2026-08-19.
rem
rem WHY encrypted: the pack contains real postings, applications, contacts, interview
rem schedules, user-written prep stories and resumes. A cloud provider must only ever
rem hold ciphertext.
rem
rem WHERE: %OneDriveConsumer% - the PERSONAL account. Deliberately not %OneDrive%,
rem which changes meaning if the work tenant signs in, and never the commercial one:
rem job-search evidence must not land in an employer's tenant.
rem
rem PASSPHRASE: read from JOBSEARCH_ARCHIVE_PASSPHRASE (setx, same convention as the
rem API keys). Unset = exit 4 and do nothing. Keep a copy of it OFF this machine - a
rem cloud backup whose only key died with the laptop is not a backup.
rem
rem No dialogs, no prompts: this runs unattended. Failures go to the log and to a
rem non-zero exit code. House rule: pure ASCII (cmd.exe parses by byte offset).
setlocal enabledelayedexpansion
cd /d %~dp0

set SEVENZIP=%ProgramFiles%\7-Zip\7z.exe
if not exist "%SEVENZIP%" (echo 7-Zip not found at %SEVENZIP% & exit /b 5)

if "%JOBSEARCH_BACKUP_DIR%"=="" (set SRCDIR=%~dp0backups) else (set SRCDIR=%JOBSEARCH_BACKUP_DIR%)
set DEST=%OneDriveConsumer%\Backups\jobsearch-offsite
set LOG=%SRCDIR%\offsite.log

if "%JOBSEARCH_ARCHIVE_PASSPHRASE%"=="" (
  echo [%date% %time%] JOBSEARCH_ARCHIVE_PASSPHRASE not set - nothing encrypted, nothing uploaded
  exit /b 4
)
if "%OneDriveConsumer%"=="" (echo [%date% %time%] personal OneDrive not configured & exit /b 6)
if not exist "%SRCDIR%\" (echo [%date% %time%] source dir unavailable: %SRCDIR% & exit /b 3)
if not exist "%DEST%\" mkdir "%DEST%" 2>nul
if not exist "%DEST%\" (echo [%date% %time%] destination unavailable: %DEST% & exit /b 3)

rem newest verified pack
set SRC=
for /f "delims=" %%f in ('dir /b /o-d "%SRCDIR%\jobsearch_evidence_backup_*.zip" 2^>nul') do (
  if not defined SRC set SRC=%%f
)
if "!SRC!"=="" (echo [%date% %time%] no evidence pack found in %SRCDIR% & exit /b 3)
set OUT=%DEST%\!SRC:.zip=.7z!

echo [%date% %time%] offsite start: !SRC! >> "%LOG%"
if exist "!OUT!" (
  echo already encrypted, skipping: !OUT! >> "%LOG%"
  goto :prune
)

rem -mhe=on encrypts the header too, so filenames inside stay hidden.
rem -mx=0 stores: the pack is already compressed, recompressing buys nothing.
rem
rem Both 7-Zip calls go through subroutines that turn delayed expansion OFF before the
rem passphrase is expanded. With it ON - and it is on, for !SRC! above - a passphrase
rem containing an exclamation mark is silently eaten by !var! processing. The verify step
rem would then reuse the SAME mangled value, so the archive would test clean and only fail
rem at restore, against the copy of the passphrase kept off this machine: a backup chain
rem that reports green for a month and is unopenable when it matters. No observed trigger
rem (checked 2026-08-23, the current passphrase has no such character) - this guards a
rem rotation, and it is written down because a guard here reads as incident-backed.
call :pack "!OUT!" "%SRCDIR%\!SRC!"
if errorlevel 1 (echo [%date% %time%] ENCRYPT FAILED >> "%LOG%" & exit /b 1)

rem An archive that has never been read back is not a backup.
call :verify "!OUT!"
if errorlevel 1 (
  echo [%date% %time%] VERIFY FAILED - deleting bad archive >> "%LOG%"
  del "!OUT!"
  exit /b 2
)
echo verified: !OUT! >> "%LOG%"

:prune
rem keep the newest 4 encrypted packs (weekly cadence = about a month of history)
for /f "skip=4 delims=" %%f in ('dir /b /o-d "%DEST%\jobsearch_evidence_backup_*.7z" 2^>nul') do (
  del "%DEST%\%%f"
  echo pruned %%f >> "%LOG%"
)
echo [%date% %time%] offsite ok >> "%LOG%"
exit /b 0

rem %1/%2 arrive already quoted from the call sites; only the passphrase needs the
rem disabledelayedexpansion scope, and neither name can contain an exclamation mark.
:pack
setlocal disabledelayedexpansion
"%SEVENZIP%" a -t7z -mhe=on -mx=0 -p"%JOBSEARCH_ARCHIVE_PASSPHRASE%" %1 %2 >> "%LOG%" 2>&1
endlocal & exit /b %errorlevel%

:verify
setlocal disabledelayedexpansion
"%SEVENZIP%" t -p"%JOBSEARCH_ARCHIVE_PASSPHRASE%" %1 >> "%LOG%" 2>&1
endlocal & exit /b %errorlevel%
