@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Creating Python 3.12 environment...
  py -3.12 -m venv .venv
  if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -c "import fastapi, uvicorn, httpx, socksio, exchange_calendars, tzdata" >nul 2>&1
if errorlevel 1 (
  echo Installing required packages...
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto failed
)
if "%NIGHTWATCH_FLOW_STREAM%"=="1" (
  ".venv\Scripts\python.exe" -c "from websockets.asyncio.client import connect" >nul 2>&1
  if errorlevel 1 (
    echo Installing the optional Flow WebSocket client...
    ".venv\Scripts\python.exe" -m pip install -r requirements-stream.txt
    if errorlevel 1 echo Optional install failed. REST will still be available.
  )
)
echo.
echo Nightwatch MANUAL trial v0.2.18. No new timed-start setting.
echo Paste the API key when asked. Input is hidden and is not saved.
echo Open http://127.0.0.1:8765 after the server starts.
echo Keep THIS window running. Closing the browser does not stop scanning.
echo Press Ctrl+C to stop. Do not leave the trial running all day.
echo.
".venv\Scripts\python.exe" -m observer.app --source nightwatch %*
if errorlevel 1 goto failed
exit /b 0
:failed
echo.
echo Start failed. Please keep this window and send a screenshot WITHOUT the API key.
pause
exit /b 1
