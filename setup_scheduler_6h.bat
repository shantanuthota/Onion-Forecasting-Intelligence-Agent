@echo off
rem Installs the Windows scheduled task that ingests UPAg Dash data every 6 hours
rem (00:00, 06:00, 12:00, 18:00 local time).
rem
rem Manual review after install: run "taskschd.msc", locate task "OnionUPAgDash6h".
rem Remove again at any time with remove_scheduler_6h.bat.
setlocal
set TASKNAME=OnionUPAgDash6h
set CONFIG=%~dp0configure_scheduler_6h.ps1
if not exist "%CONFIG%" (
  echo configure_scheduler_6h.ps1 not found next to this script.
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%CONFIG%"
if errorlevel 1 (
  echo Failed to configure scheduled task %TASKNAME%.
  exit /b 1
)
echo Configured scheduled task %TASKNAME%: runs every 6 hours at 00:00, 06:00, 12:00, 18:00.
echo Every run is audited in ingestion_log inside data\onion_forecasting_v4.sqlite3;
echo raw snapshots land in data\snapshots\.
echo The task runs pull_upag_dash.py directly and updates SQLite; it does not call the local FastAPI HTTP API.
endlocal
