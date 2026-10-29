@echo off
rem One command for everything: starts the FastAPI backend AND enables the
rem 6-hourly UPAg Dash ingestion task (OnionUPAgDash6h) while the backend runs.
rem Stopping the backend (Ctrl+C) automatically disables the task again.
rem
rem Interpreter choice: forecasts need torch, so this picks the project .venv
rem only if torch is installed there; otherwise it uses the system Python
rem (the interpreter your full backend normally runs on). The ingestion task
rem itself always uses the project .venv and does not need torch.
rem Extra args are passed through, e.g.: backend --port 8001
cd /d %~dp0
set PY=python
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe -c "import torch" >nul 2>&1
  if not errorlevel 1 set PY=.venv\Scripts\python.exe
)
%PY% backend.py %*
