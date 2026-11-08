@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  call Setup-ClearFrame.cmd
  if errorlevel 1 exit /b 1
)
".venv\Scripts\python.exe" launch_studio.py
if errorlevel 1 pause
