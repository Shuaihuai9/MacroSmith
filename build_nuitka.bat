@echo off
rem MacroSmith - Nuitka standalone build (anti-decompile)
rem Usage: run from project root:  build_nuitka.bat
rem Output: dist\MacroSmith\MacroSmith.exe  (feed dist\MacroSmith\* to Inno Setup)
rem
rem REQUIRED toolchain (verified working combo, do NOT upgrade blindly):
rem   nuitka==4.2.1        (4.3 has a segfault regression: Qt subclass creation)
rem   PySide6 6.8.x        (6.11 also triggers the same segfault)
rem   Python 3.13          (mingw64 backend unsupported on 3.13+; zig/msvc only)

python -m nuitka --standalone --enable-plugin=pyside6 ^
  --windows-console-mode=disable ^
  --windows-uac-admin ^
  --windows-icon-from-ico=assets/icon.ico ^
  --include-data-files=assets/icon.ico=assets/icon.ico ^
  --include-package=pynput ^
  --company-name=Shuaihuai9 ^
  --product-name=MacroSmith ^
  --file-version=1.1.0 ^
  --product-version=1.1.0 ^
  --file-description="MacroSmith macro tool" ^
  --assume-yes-for-downloads ^
  --remove-output ^
  --output-dir=build_nuitka ^
  --output-filename=MacroSmith.exe ^
  main.py

if errorlevel 1 (
  echo.
  echo [ERROR] Nuitka build failed.
  exit /b 1
)

rem Align output to dist\MacroSmith (keeps installer\MacroSmith.iss unchanged)
if exist dist\MacroSmith rmdir /s /q dist\MacroSmith
robocopy build_nuitka\main.dist dist\MacroSmith /E >nul
if errorlevel 8 (
  echo [ERROR] Copy to dist\MacroSmith failed.
  exit /b 1
)

echo.
echo [OK] Build done: dist\MacroSmith\MacroSmith.exe
echo Verify with:  dist\MacroSmith\MacroSmith.exe --check
