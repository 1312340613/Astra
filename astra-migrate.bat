@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo   === Astra - Directory Migration ===
echo.
if not exist "%CD%\.agent_system" (
    echo   [OK] No legacy .agent_system directory found.
    goto :done
)
if exist "%CD%\.venv\Scripts\python.exe" (
    set "MIGRATE_PYTHON=%CD%\.venv\Scripts\python.exe"
) else (
    set "MIGRATE_PYTHON=python"
)
echo   Merging .agent_system into .astra...
"%MIGRATE_PYTHON%" "%CD%\scripts\migrate_legacy_astra_state.py"
if errorlevel 1 (
    echo   [ERROR] Migration failed. The legacy directory was left untouched.
    goto :done
)
echo.
echo   [OK] Migration complete.
echo   [INFO] .agent_system was kept as a rollback backup and is ignored by Git.
:done
echo.
pause
