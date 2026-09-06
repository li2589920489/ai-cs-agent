@echo off
rem ============================================================
rem  AI E-commerce Customer Service Agent - One-click Stopper
rem  Usage: double-click this file (stop.bat)
rem  Stops backend (:8000) and frontend (:3000) started by start.bat
rem ============================================================
title AI E-commerce Customer Service Agent - Stopper

echo ============================================================
echo   Stopping services...
echo ============================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ports=8000,3000; $pids = Get-NetTCPConnection -LocalPort $ports -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique; if ($pids) { foreach ($p in $pids) { Write-Host ('  Stopping PID ' + $p); Stop-Process -Id $p -Force -ErrorAction SilentlyContinue } } else { Write-Host '  No service found (already stopped).' }"

echo.
echo ============================================================
echo   Done. Services stopped.
echo   You can now close the two leftover windows.
echo ============================================================
echo.
pause
