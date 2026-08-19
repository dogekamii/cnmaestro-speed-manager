@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\pyinstaller --noconfirm --clean --onefile --windowed --name "Operations-Toolkit-v1.2.2" cnmaestro_speed_manager.py
pause
