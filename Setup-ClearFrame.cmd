@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
  py -3 setup_studio.py %*
) else (
  python setup_studio.py %*
)
if errorlevel 1 (
  echo.
  echo Setup failed. Install 64-bit Python 3.10-3.13 from python.org, then retry.
  pause
  exit /b 1
)
echo.
echo Ready. Double-click Start-ClearFrame.cmd to open ClearFrame.
pause
