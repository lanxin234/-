@echo off
REM ============================================================
REM  Backup runtime data (chain + accounts)
REM
REM  Double-click to run. Safe while the server is running:
REM  uses SQLite's backup API, not a raw file copy.
REM
REM  This file is intentionally ASCII-only.
REM ============================================================

chcp 65001 >nul
setlocal

cd /d "%~dp0"

set "PYTHONIOENCODING=utf-8"
set "PY=C:\Users\25191\.venvs\base\Scripts\python.exe"
if not exist "%PY%" (
    set "PY=python"
)

echo.
echo ============================================================
echo   Backup data
echo ============================================================
echo.

"%PY%" -X utf8 scripts\backup_data.py --with-uploads --keep 10

echo.
echo   Done. Backup folder: backups\
echo.
pause
