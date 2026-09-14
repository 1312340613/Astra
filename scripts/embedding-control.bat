@echo off
setlocal
title Astra Embedding Control
if /I "%~1"=="start" goto start
if /I "%~1"=="stop" goto stop
if /I "%~1"=="status" goto status

:menu
cls
echo ========================================
echo      Astra Embedding - llama.cpp
echo ========================================
echo   1. Start
echo   2. Stop
echo   3. Status
echo   4. Exit
echo.
choice /C 1234 /N /M "Select [1-4]: "
if errorlevel 4 goto end
if errorlevel 3 goto status
if errorlevel 2 goto stop
if errorlevel 1 goto start
goto end

:start
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-embedding-windows.ps1" -Action start
goto done

:stop
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-embedding-windows.ps1" -Action stop
goto done

:status
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-embedding-windows.ps1" -Action status
goto done

:done
if not "%~1"=="" exit /b %errorlevel%
echo.
pause
goto menu

:end
endlocal
