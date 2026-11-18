@echo off
setlocal
cd /d "%~dp0"
set "PYTHON="
where py >nul 2>nul
if not errorlevel 1 set "PYTHON=py -3"
if not defined PYTHON (
  where python >nul 2>nul
  if not errorlevel 1 set "PYTHON=python"
)
if not defined PYTHON (
  echo Python not found. Install Python 3.13 or 3.14 from python.org, check Add Python to PATH.
  pause
  exit /b 1
)
%PYTHON% -m venv .venv
if errorlevel 1 goto :failed
call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
if errorlevel 1 goto :failed
python -m pip install "PySide6>=6.8,<7" "pyinstaller>=6.12,<7"
if errorlevel 1 goto :failed
python -m PyInstaller --noconfirm --clean --onefile --windowed --name "AlterEgo Desktop" --icon "app.ico" --add-data "app.ico;." "AlterEgo Desktop.pyw"
if errorlevel 1 goto :failed
echo.
echo SUCCESS: dist\AlterEgo Desktop.exe
echo Copy only the EXE to any Windows 11 PC.
pause
exit /b 0
:failed
echo.
echo Build failed. Read errors above.
pause
exit /b 1
