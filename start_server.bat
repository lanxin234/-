@echo off
REM ============================================================
REM  Blockchain Second-hand Market - Start Server
REM
REM  Double-click to run.
REM  KEEP THIS WINDOW OPEN while others are using the site.
REM  Closing it stops the server for everyone.
REM
REM  Note: this file is intentionally ASCII-only.
REM  Batch files with non-ASCII characters get garbled by
REM  Windows code-page conversion.
REM ============================================================

chcp 65001 >nul
setlocal

cd /d "%~dp0"

REM Load local secrets (ADMIN_KEY / INVITE_CODE) if config.env exists.
REM config.env is gitignored - never put real keys in this .bat file,
REM because this file IS committed to a public repository.
if exist "config.env" (
    for /f "usebackq tokens=1,* delims==" %%A in ("config.env") do set "%%A=%%B"
)

set "PYTHONIOENCODING=utf-8"

REM Prefer the project venv; fall back to system python.
set "PY=C:\Users\25191\.venvs\base\Scripts\python.exe"
if not exist "%PY%" (
    set "PY=python"
)

echo.
echo ============================================================
echo   Blockchain Second-hand Market - Backend Server
echo ============================================================
echo.
echo   Python  : %PY%
echo   Port    : 5000
echo.
echo   When it finishes booting you will see:
echo     - http://127.0.0.1:5000        (this computer)
echo     - http://192.168.x.x:5000      (share this with classmates)
echo.
echo   If classmates cannot connect, Windows Firewall is probably
echo   blocking the port. Run this ONCE as Administrator:
echo.
echo     netsh advfirewall firewall add rule name="BlockchainMarket"^
echo  dir=in action=allow protocol=TCP localport=5000
echo.
echo   Press Ctrl+C to stop the server.
echo ============================================================
echo.

"%PY%" -X utf8 backend\app.py

echo.
echo   Server stopped.
pause
