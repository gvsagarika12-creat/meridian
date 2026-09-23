@echo off
REM Use this when you cannot sign in. Close the app first.

cd /d "%~dp0"

echo ============================================
echo   Meridian  -  reset a password
echo ============================================
echo.
echo Accounts on this system:
echo.

python admin.py list

echo.
set /p EMAIL="Email to reset (copy one from above): "
echo.

python admin.py set-password "%EMAIL%"

echo.
pause
