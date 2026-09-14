@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
set "ASTRA_PYTHON=%~dp0..\.venv\Scripts\python.exe"
if not exist "%ASTRA_PYTHON%" set "ASTRA_PYTHON=python"
if defined PYTHON set "ASTRA_PYTHON=%PYTHON%"
"%ASTRA_PYTHON%" "%~dp0check_163.py" %*
exit /b %errorlevel%
