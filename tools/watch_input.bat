@echo off
rem Double-click helper for tools\watch_input.py -- keyboard event capture.
rem
rem This file intentionally does NOT handle UAC. It used to: the bat spawned an
rem elevated copy with "Start-Process -Verb RunAs" and then exited right away,
rem so an unnoticed UAC prompt looked exactly like "the window flashes and
rem closes" (reported 2026-11-12). Elevation now happens inside
rem watch_input.py, where ShellExecuteW's return code tells "prompt accepted"
rem apart from "user clicked No", and the window stays open to say so.
chcp 65001 >nul
cd /d "%~dp0.."

set "PY=python"
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"

%PY% "tools\watch_input.py" %*
echo.
echo ------------------------------------------------------------
echo If the messages above flew by, run it from a command prompt
echo to see the error and keep it on screen:
echo     py -3 tools\watch_input.py
echo ------------------------------------------------------------
pause
