@echo off
rem Launch the PCM -> DSD command builder GUI.
setlocal
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  )
)
if not defined PY (
  echo Python not found. Install Python 3.8+ or edit this file to point at python.exe.
  pause
  exit /b 1
)

start "" "%PY%" "%~dp0gui.py"
endlocal
