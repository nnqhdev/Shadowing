@echo off
chcp 65001 >nul
cd /d "%~dp0"
start "" "%~dp0.venv\Scripts\pythonw.exe" -X utf8 "%~dp0desktop_app.py"
