@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
set "MIGRATE_PYTHON=%~dp0..\.venv\Scripts\python.exe"
if not exist "%MIGRATE_PYTHON%" set "MIGRATE_PYTHON=python"
if defined PYTHON set "MIGRATE_PYTHON=%PYTHON%"
cd /d "%~dp0.." || exit /b 1
"%MIGRATE_PYTHON%" "%~dp0migrate_legacy_astra_state.py" %*
exit /b %errorlevel%
