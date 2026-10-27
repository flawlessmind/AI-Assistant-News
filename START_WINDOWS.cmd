@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
    python -X utf8 scripts\start.py --port 8002
) else (
    py -3.12 -X utf8 scripts\start.py --port 8002
)
if errorlevel 1 pause
