@echo off
if not exist .venv\Scripts\python.exe (
  echo Run setup_windows.bat first.
  exit /b 1
)
if not defined API_HOST set API_HOST=127.0.0.1
.venv\Scripts\python.exe -m uvicorn main:app --host %API_HOST% --port 8000
