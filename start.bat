@echo off
cd /d "%~dp0"
rem Launcher for the development build.
rem
rem ELEVATION IS NOT DONE HERE. main.py does it itself (relaunch_as_admin(),
rem ShellExecuteW "runas"), and that is strictly better: it can read the return
rem code, so "UAC accepted" and "user clicked No" are distinguishable, and the
rem window stays put instead of vanishing.
rem
rem This file used to elevate itself with:
rem     powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
rem     exit /b
rem The parent window exits immediately after spawning the elevated copy, so if
rem the UAC prompt goes unnoticed the whole thing looks like "the window flashes
rem and closes" with no error at all (same bug as tools\watch_input.bat,
rem 2026-11-12). Tencent (WeGame / QQ Game Hall) does run some processes at high
rem integrity and UIPI blocks input from a lower one, so admin rights are still
rem required -- main.py just handles the asking.

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 main.py
) else (
    python main.py
)
pause
