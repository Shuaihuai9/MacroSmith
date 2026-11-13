@echo off
setlocal
rem MacroSmith - Nuitka standalone build (anti-decompile)
rem Usage: run from project root:  build_nuitka.bat   (double-click also works)
rem Output: dist\MacroSmith\MacroSmith.exe  (feed dist\MacroSmith\* to Inno Setup)
rem
rem REQUIRED toolchain (verified working combo, do NOT upgrade blindly):
rem   nuitka==4.2.1        (4.3 has a segfault regression: Qt subclass creation)
rem   PySide6 6.8.x        (6.11 also triggers the same segfault)
rem   Python 3.13          (mingw64 backend unsupported on 3.13+; zig/msvc only)
rem
rem WHY `py -3` AND NOT bare `python`:
rem   Double-clicking this file used to close instantly. Bare `python` resolves
rem   to whatever sits first on PATH, and on this machine that is a bundled
rem   interpreter WITHOUT nuitka -- so the very first command failed and the
rem   window vanished before the message could be read. Now the interpreter is
rem   picked explicitly (and verified), and every exit path pauses.

cd /d "%~dp0"

rem ---- 1. pick an interpreter that really has nuitka + PySide6 ----
set "PY=py -3"
%PY% -c "import nuitka, PySide6" >nul 2>&1
if errorlevel 1 set "PY=python"
%PY% -c "import nuitka, PySide6" >nul 2>&1
if errorlevel 1 goto :nodep

echo [1/4] Interpreter in use:
%PY% -c "import sys; print('      ' + sys.executable)"
echo [2/4] Nuitka version:
%PY% -m nuitka --version 2>nul
echo.

rem ---- 2. preflight: files the build depends on ----
if not exist "main.py" (
  echo [ERROR] main.py not found. Run this from the project root.
  goto :fail
)
if not exist "assets\icon.ico" (
  echo [ERROR] assets\icon.ico not found; Nuitka needs it.
  goto :fail
)

rem ---- 3. build ----
echo [3/4] Building (first build takes 5-15 minutes, keep this window open)...
echo.
%PY% -m nuitka --standalone --enable-plugin=pyside6 ^
  --windows-console-mode=disable ^
  --windows-uac-admin ^
  --windows-icon-from-ico=assets/icon.ico ^
  --include-data-files=assets/icon.ico=assets/icon.ico ^
  --include-package=pynput ^
  --company-name=Shuaihuai9 ^
  --product-name=MacroSmith ^
  --file-version=1.2.0 ^
  --product-version=1.2.0 ^
  --file-description="MacroSmith macro tool" ^
  --assume-yes-for-downloads ^
  --remove-output ^
  --output-dir=build_nuitka ^
  --output-filename=MacroSmith.exe ^
  main.py

if errorlevel 1 (
  echo.
  echo [ERROR] Nuitka build failed. Paste the last 20 lines of output.
  goto :fail
)

rem ---- 4. align output to dist\MacroSmith (keeps installer\MacroSmith.iss unchanged) ----
if exist dist\MacroSmith rmdir /s /q dist\MacroSmith
robocopy build_nuitka\main.dist dist\MacroSmith /E >nul
if errorlevel 8 (
  echo [ERROR] Copy to dist\MacroSmith failed.
  goto :fail
)

echo.
echo [OK] Build done: dist\MacroSmith\MacroSmith.exe
echo Verify:  dist\MacroSmith\MacroSmith.exe --check
echo.
pause
exit /b 0

:nodep
echo [ERROR] No Python found that has BOTH nuitka and PySide6.
echo.
echo Neither "py -3" nor "python" can import nuitka + PySide6.
echo Install them into the interpreter you want to build with:
echo     py -3 -m pip install "nuitka==4.2.1" "PySide6==6.8.3"
goto :fail

:fail
echo.
pause
exit /b 1
