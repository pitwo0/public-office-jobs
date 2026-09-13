@echo off
setlocal
cd /d "%~dp0"
call refresh.cmd
set "JOB_REFRESH_EXIT=%ERRORLEVEL%"
start "" "%~dp0index.html"
if not "%JOB_REFRESH_EXIT%"=="0" pause
exit /b %JOB_REFRESH_EXIT%
