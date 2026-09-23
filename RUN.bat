@echo off
REM Starts the PostgreSQL container, then the app with no console window.
REM Errors are written to desktop.log in this folder.

cd /d "%~dp0"

REM The database now lives in Docker, so it has to be up before the app starts.
REM "start" on an existing container is a no-op if it is already running.
docker start ipmg-postgres >nul 2>&1
if errorlevel 1 (
  echo.
  echo   Could not start the database container "ipmg-postgres".
  echo.
  echo   Open Docker Desktop and wait for it to say "Engine running",
  echo   then run this file again.
  echo.
  pause
  exit /b 1
)

REM Postgres accepts connections a second or two after the container starts.
for /l %%i in (1,1,20) do (
  docker exec ipmg-postgres pg_isready -U ipmg -d ipmg_intake >nul 2>&1
  if not errorlevel 1 goto ready
  timeout /t 1 /nobreak >nul
)

echo   The database did not become ready. See: docker logs ipmg-postgres
pause
exit /b 1

:ready
start "" pythonw.exe desktop.py
exit
