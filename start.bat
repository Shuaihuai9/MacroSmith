@echo off
cd /d %~dp0
rem Elevate to administrator via UAC. Tencent (WeGame / QQ Game Hall) runs
rem some processes with higher integrity, and UIPI blocks key-state reads
rem from a non-elevated process. Admin rights fix click/key recording.
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator privileges ...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
rem Use the py launcher to pick the newest Python 3.x on this machine.
rem This project needs Python 3.10 or newer.
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 main.py
) else (
    python main.py
)
pause
