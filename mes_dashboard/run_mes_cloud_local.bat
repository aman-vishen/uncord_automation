@echo off
setlocal
cd /d "%~dp0"
if not defined INGEST_API_KEY set INGEST_API_KEY=local-development-key
if not defined DASHBOARD_USERNAME set DASHBOARD_USERNAME=admin
if not defined DASHBOARD_PASSWORD set DASHBOARD_PASSWORD=admin
if not defined PORT set PORT=8080
python app.py
pause
