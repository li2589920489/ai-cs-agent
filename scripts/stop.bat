@echo off
rem ============================================================
rem  AI E-commerce Customer Service Agent - One-click Stopper
rem  Usage: double-click this file (stop.bat)
rem  Stops backend (:8000) and frontend (:3000) started by start.bat,
rem  and auto-closes the two cmd windows spawned by start.bat.
rem ============================================================
title AI E-commerce Customer Service Agent - Stopper

echo ============================================================
echo   Stopping services...
echo ============================================================
echo.

echo [1/2] Stopping backend (:8000) and frontend (:3000) processes...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ports=8000,3000; $pids = Get-NetTCPConnection -LocalPort $ports -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique; if ($pids) { foreach ($p in $pids) { Write-Host ('  Stopping service PID ' + $p); Stop-Process -Id $p -Force -ErrorAction SilentlyContinue } } else { Write-Host '  No service found (already stopped).' }"

echo.
echo [2/2] Closing the two cmd windows spawned by start.bat...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$titles = 'Backend FastAPI :8000','Frontend Next.js :3000'; $closed = 0; foreach ($t in $titles) { $p = Get-Process cmd -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowTitle -eq $t } | Select-Object -First 1; if ($p) { Write-Host ('  Closing window: ' + $t); Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue; $closed++ } }; if ($closed -eq 0) { Write-Host '  No launcher windows found (already closed).' }"

echo.
echo ============================================================
echo   Done. All services and launcher windows closed.
echo ============================================================
echo.
pause
