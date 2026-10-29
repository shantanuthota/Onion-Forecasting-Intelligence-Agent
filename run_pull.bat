@echo off
rem Scheduled-task wrapper for the 6-hourly UPAg Dash ingestion.
rem This file is what the Windows Task Scheduler task "OnionUPAgDash6h" executes.
cd /d %~dp0
if not exist .venv\Scripts\python.exe (
  echo .venv missing - run setup_windows.bat first.
  exit /b 1
)
.venv\Scripts\python.exe pull_upag_dash.py --once --json
