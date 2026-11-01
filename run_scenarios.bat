@echo off
setlocal
cd /d "%~dp0"
python run_scenarios.py
set "scenario_exit=%errorlevel%"
pause
exit /b %scenario_exit%
