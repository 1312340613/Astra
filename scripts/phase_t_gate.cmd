@echo off
setlocal
"%~dp0..\.venv\Scripts\python.exe" "%~dp0phase_t_gate.py" %*
exit /b %ERRORLEVEL%
