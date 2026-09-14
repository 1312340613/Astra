@echo off
chcp 65001 >nul
set "ASTRA_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%ASTRA_PYTHON%" set "ASTRA_PYTHON=python"
"%ASTRA_PYTHON%" "%~dp0scripts\check_163.py" %*
