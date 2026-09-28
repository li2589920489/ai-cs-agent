@echo off
rem ============================================================
rem  AI E-commerce Customer Service Agent - One-click Launcher
rem  Usage: double-click this file (start.bat)
rem  Backend : http://127.0.0.1:8000   (health check /health)
rem  Frontend: http://127.0.0.1:3000   (open this in browser)
rem ============================================================
title AI E-commerce Customer Service Agent - Launcher

rem Project root is one level up from this script's directory.
rem Use direct string concatenation (no cd side effects) so the path
rem is unambiguous regardless of the caller's current directory.
set "ROOT=%~dp0.."

echo ============================================================
echo   AI E-commerce Customer Service Agent
echo ============================================================
echo.

echo [1/4] Cleaning frontend cache (.next)...
if exist "%ROOT%\ui\.next" (
    rmdir /s /q "%ROOT%\ui\.next"
    echo       .next cleared.
) else (
    echo       .next not found, skip.
)

echo [2/4] Starting backend (FastAPI on :8000)...
rem /D sets the working directory, avoiding the cmd /k "cd /d ..." quoting trap.
start "Backend FastAPI :8000" /D "%ROOT%\python-backend" cmd /k ".venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000"

echo [3/4] Waiting for backend health check (up to 30s)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ready=$false; for($i=1;$i -le 30;$i++){ try{ $r=Invoke-WebRequest -Uri 'http://127.0.0.1:8000/health' -UseBasicParsing -TimeoutSec 2; if($r.StatusCode -eq 200){ $ready=$true; Write-Host ('  Backend ready after ' + $i + 's.'); break } } catch {}; Start-Sleep -Seconds 1 }; if(-not $ready){ Write-Host '  Backend did not become ready in 30s. Aborting.'; exit 1 }"
if errorlevel 1 (
    echo.
    echo   Backend failed to start. Check the backend window for errors.
    pause
    exit /b 1
)

echo [4/4] Starting frontend (Next.js on :3000)...
rem /D sets the working directory; same trick as backend above.
start "Frontend Next.js :3000" /D "%ROOT%\ui" cmd /k "npm run dev:next"

echo.
echo ============================================================
echo   Done. Two new windows are open:
echo     Backend : http://127.0.0.1:8000/health
echo     Frontend: http://127.0.0.1:3000
echo   Open http://127.0.0.1:3000 in your browser.
echo   Run stop.bat to shut everything down cleanly.
echo ============================================================
echo.
pause
