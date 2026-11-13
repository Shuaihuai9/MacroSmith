@echo off
rem Double-click helper for tools\probe_target.py -- READ-ONLY channel probe.
rem
rem This is a diagnosis, not a capture: it prints the window class, both
rem integrity levels, and whether each of the four input channels can write.
rem The keystroke sniffer is tools\watch_input.bat -- a different tool.
rem
rem No UAC handling here on purpose (see watch_input.bat for the post-mortem:
rem a bat that spawns an elevated copy and exits looks like a flash when the
rem UAC prompt goes unnoticed). probe_target.py elevates itself and keeps the
rem window open to explain what happened.
chcp 65001 >nul
cd /d "%~dp0.."

set "PY=python"
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"

echo ============================================================
echo  READ-ONLY DIAGNOSIS  (probe_target.py)
echo.
echo  Does NOT record keystrokes. It answers: which input channel
echo  can write into this window? It also compares both integrity
echo  levels, because a lower-privilege probe always reports
echo  "everything blocked" -- which is a test bug, not a fact.
echo.
echo  Want to capture another tool's key events instead?
echo  Use tools\watch_input.bat.
echo ============================================================
echo.

%PY% "tools\probe_target.py" --test %*
echo.
echo ------------------------------------------------------------
echo If the messages above flew by, run it from a command prompt
echo to see the error and keep it on screen:
echo     py -3 tools\probe_target.py --test --now
echo ------------------------------------------------------------
pause
