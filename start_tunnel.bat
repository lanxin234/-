@echo off
REM ============================================================
REM  Expose the local server to the internet (Cloudflare Tunnel)
REM
REM  Start the backend FIRST (start_server.bat), then run this.
REM  It prints a https://xxx.trycloudflare.com address - share it.
REM
REM  Notes:
REM   - No account needed; the URL is random and CHANGES every run.
REM   - Closing this window cuts the public link.
REM   - The local server keeps running; only the tunnel stops.
REM   - Do NOT rely on this for real money. Demo chain only.
REM
REM  This file is intentionally ASCII-only.
REM ============================================================

chcp 65001 >nul
setlocal

cd /d "%~dp0"

set "CF=C:\Users\25191\.local\bin\cloudflared.exe"
if not exist "%CF%" (
    set "CF=cloudflared"
)

echo.
echo ============================================================
echo   Cloudflare Tunnel  -^>  http://127.0.0.1:5000
echo ============================================================
echo.
echo   Look for a line like:
echo     https://xxxx-xxxx.trycloudflare.com
echo   Share that address. Press Ctrl+C to stop the tunnel.
echo.

"%CF%" tunnel --url http://127.0.0.1:5000 --no-autoupdate

echo.
echo   Tunnel closed.
pause
