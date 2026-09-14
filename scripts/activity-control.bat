@echo off
setlocal
title Astra Windows Activity
if /I "%~1"=="start" goto start
if /I "%~1"=="stop" goto stop
if /I "%~1"=="pause" goto pause_recording
if /I "%~1"=="resume" goto resume
if /I "%~1"=="status" goto status
:menu
cls
echo Astra Windows Activity - foreground app and window title
echo Summaries use your configured LLM. History retention: 30 days.
echo.
echo 1. Start
echo 2. Pause
echo 3. Resume
echo 4. Stop
echo 5. Status
echo 6. Exit menu (keep recorder running)
echo 7. Clear activity history (recorder must be stopped)
echo 8. Copy browser extension pairing code
choice /C 12345678 /N /M "Select [1-8]: "
if errorlevel 8 goto browser
if errorlevel 7 goto clear_history
if errorlevel 6 exit /b
if errorlevel 5 goto status
if errorlevel 4 goto stop
if errorlevel 3 goto resume
if errorlevel 2 goto pause_recording
if errorlevel 1 goto start
:start
set "action=start"
goto run
:stop
set "action=stop"
goto run
:pause_recording
set "action=pause"
goto run
:resume
set "action=resume"
goto run
:status
set "action=status"
goto run
:clear_history
set "action=clear"
goto run
:browser
set "action=browser"
:run
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows-activity.ps1" -Action %action%
if not "%~1"=="" exit /b %errorlevel%
pause
goto menu
