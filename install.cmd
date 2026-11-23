@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] Creating Python virtual environment...
  py -3.12 -m venv .venv 2>nul
  if not exist ".venv\Scripts\python.exe" python -m venv .venv
  if not exist ".venv\Scripts\python.exe" (
    echo ❌ Could not create the virtual environment. Is Python 3.10+ installed?
    exit /b 1
  )
)

echo [2/3] Installing dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo ❌ pip install failed.
  exit /b 1
)

if not exist "config.yaml" (
  echo [3/3] Creating config.yaml...
  copy /Y "config.example.yaml" "config.yaml" >nul
) else (
  echo [3/3] config.yaml already exists - keeping it.
)

echo.
echo ✅ Install complete.
echo    Start the API : api.cmd
echo    Gemini login  : login.cmd
echo    Diagnostics   : doctor.cmd
echo.
