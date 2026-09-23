@echo off
REM Nightly backup, run by Windows Task Scheduler.
REM
REM Everything is appended to backups\backup.log, because a scheduled task that
REM fails silently is worse than no scheduled task at all - you believe you have
REM backups right up until the day you need one.

cd /d "%~dp0"
if not exist "backups" mkdir "backups"

set LOG=backups\backup.log
set PY=C:\Users\admin\AppData\Local\Programs\Python\Python312\python.exe

echo. >> "%LOG%"
echo ================================================================ >> "%LOG%"
echo %DATE% %TIME%  nightly backup starting >> "%LOG%"

REM The local database lives in Docker. If Docker Desktop is not running there
REM is nothing to dump, and saying so is more useful than a pg_dump stack trace.
docker info >nul 2>&1
if errorlevel 1 (
  echo   SKIPPED - Docker Desktop is not running, the local database is offline >> "%LOG%"
  goto cloud
)

"%PY%" backup.py >> "%LOG%" 2>&1
if errorlevel 1 (
  echo   LOCAL BACKUP FAILED - see the lines above >> "%LOG%"
) else (
  echo   local backup ok >> "%LOG%"
)

:cloud
REM The hosted database needs credentials pulled from Vercel. If they are absent
REM the cloud step is skipped; the local backup above still happened.
if not exist ".vercel\.env.production" (
  echo   SKIPPED cloud - no .vercel\.env.production ^(run: npx vercel env pull^) >> "%LOG%"
  goto done
)

"%PY%" backup.py --cloud >> "%LOG%" 2>&1
if errorlevel 1 (
  echo   CLOUD BACKUP FAILED - see the lines above >> "%LOG%"
) else (
  echo   cloud backup ok >> "%LOG%"
)

:done
echo %DATE% %TIME%  finished >> "%LOG%"
