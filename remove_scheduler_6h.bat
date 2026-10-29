@echo off
rem Removes the 6-hourly UPAg Dash ingestion scheduled task created by setup_scheduler_6h.bat.
schtasks /delete /tn "OnionUPAgDash6h" /f
if errorlevel 1 (
  echo Task "OnionUPAgDash6h" was not found (already removed?).
  exit /b 1
)
echo Removed scheduled task "OnionUPAgDash6h".
