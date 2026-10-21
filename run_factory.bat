@echo off
cd /d "%~dp0"
start "" "http://127.0.0.1:8088"
.\.venv\Scripts\python.exe app.py
pause
