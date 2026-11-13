@echo off
cd /d "%~dp0.."
rem DEBUG launcher: same as start.bat but with --debug.
rem
rem Why this exists: when a hotkey "does nothing", the log used to be blank and
rem there was no way to tell the two cases apart -- (1) the low level keyboard
rem hook never got the event at all (a security input component or a driver ate
rem it), (2) the hook got it but the action table had no binding for it.
rem With --debug the hook logs EVERY key it sees, so one press answers that.
rem Do not use this for normal work: typing for a minute fills the log.
rem
echo ============================================================
echo   MacroSmith - DEBUG MODE
echo   Hook logs every key event. Close this window when done.
echo   Log: %APPDATA%\MacroSmith\logs\macrosmith.log
echo ============================================================
echo.

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 main.py --debug
) else (
    python main.py --debug
)
echo.
echo ---- MacroSmith exited (code %errorlevel%) ----
pause
