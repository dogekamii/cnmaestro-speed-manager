@echo off
setlocal
cd /d "%~dp0"
title Operations Toolkit Launcher
echo Operations Toolkit launcher > launcher.log
echo Started: %date% %time% >> launcher.log
where py >nul 2>&1 && (set "PY=py") || (where python >nul 2>&1 && (set "PY=python") || goto :nopy)
if not exist ".venv\Scripts\python.exe" %PY% -m venv .venv >> launcher.log 2>&1
if errorlevel 1 goto :failed
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt >> launcher.log 2>&1
if errorlevel 1 goto :failed
".venv\Scripts\python.exe" cnmaestro_speed_manager.py >> launcher.log 2>&1
if errorlevel 1 goto :failed
exit /b 0
:nopy
echo ERROR: Python was not found. & pause & exit /b 1
:failed
echo ERROR: Startup failed. Review launcher.log.
type launcher.log
pause
