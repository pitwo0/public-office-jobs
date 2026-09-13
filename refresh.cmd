@echo off
setlocal
cd /d "%~dp0"
py -3 collector\refresh.py
set "JOB_REFRESH_EXIT=%ERRORLEVEL%"
if not "%JOB_REFRESH_EXIT%"=="0" echo Refresh incomplete. See data\refresh_status.json and data\refresh_error.json.
exit /b %JOB_REFRESH_EXIT%
