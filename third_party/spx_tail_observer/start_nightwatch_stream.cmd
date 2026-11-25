@echo off
setlocal
set "NIGHTWATCH_FLOW_STREAM=1"
call "%~dp0start_nightwatch.cmd" %*
exit /b %errorlevel%
