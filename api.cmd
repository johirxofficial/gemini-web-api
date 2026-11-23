@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo ❌ Run install.cmd first.
  exit /b 1
)
if not exist "config.yaml" copy /Y "config.example.yaml" "config.yaml" >nul
".venv\Scripts\python.exe" -m gwa serve %*
