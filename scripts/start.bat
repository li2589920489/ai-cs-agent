@echo off
rem ============================================================
rem  AI E-commerce Customer Service Agent - One-click Launcher
rem  Usage: double-click this file (start.bat)
rem  Backend : http://127.0.0.1:8000   (health check /health)
rem  Frontend: http://127.0.0.1:3000   (open this in browser)
rem ============================================================
title AI E-commerce Customer Service Agent - Launcher

set "ROOT=%~dp0"

echo ============================================================
echo   AI E-commerce Customer Service Agent
echo ============================================================
echo.

echo [1/3] Cleaning frontend cache (.next)...
if exist "%ROOT%ui\.next" (
    rmdir /s /q "%ROOT%ui\.next"
    echo       .next cleared.
) else (
    echo       .next not found, skip.
)

echo [2/3] Starting backend (FastAPI on :8000)...
start "Backend FastAPI :8000" cmd /k "cd /d "%ROOT%python-backend" && .venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000"

echo [3/3] Starting frontend (Next.js on :3000)...
start "Frontend Next.js :3000" cmd /k "cd /d "%ROOT%ui" && npm run dev:next"

echo.
echo ============================================================
echo   Done. Two new windows are opening:
echo     Backend : http://127.0.0.1:8000/health
echo     Frontend: http://127.0.0.1:3000
echo   Open http://127.0.0.1:3000 in your browser.
echo   Close the two windows to stop the services.
echo ============================================================
echo.
pause
