@echo off
rem Double-click helper for tools\try_type.py -- the "why does the reference
rem tool get through" A/B test: same channels, different inter-key delays.
rem
rem Elevation is done by the Python script (see watch_input.bat for why a bat
rem must not do it: it exits before the UAC prompt is answered, which looks
rem exactly like a crash).
chcp 65001 >nul
cd /d "%~dp0.."

set "PY=python"
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"

%PY% "tools\try_type.py" %*
echo.
pause
