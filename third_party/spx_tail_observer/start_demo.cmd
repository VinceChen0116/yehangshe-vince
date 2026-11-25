@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  py -3.12 -m venv .venv
  if errorlevel 1 goto failed
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto failed
)
echo DEMO ONLY. All prices and activity are synthetic.
echo Open http://127.0.0.1:8765
".venv\Scripts\python.exe" -m observer.app --source demo
pause
exit /b 0
:failed
pause
exit /b 1
