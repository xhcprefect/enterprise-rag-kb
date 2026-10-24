@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Starting ingest worker...
".venv\Scripts\python.exe" -X utf8 worker.py
pause
