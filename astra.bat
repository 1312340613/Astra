@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
set "PYTHONUTF8=1"
set "ASTRA_BOOTSTRAP="
rem Do not let an inherited variable shadow CMD's dynamic exit status.
set "ERRORLEVEL="
if defined PYTHON (
    set "ASTRA_BOOTSTRAP=%PYTHON%"
    goto ready
)
rem Prefer this installation's base interpreter. Maintenance must not run
rem inside the .venv that setup/update may replace.
set "ASTRA_CANDIDATE=%~dp0.venv\Scripts\python.exe"
if exist "%ASTRA_CANDIDATE%" call :probe_python
if defined ASTRA_BOOTSTRAP goto ready
where py >nul 2>&1
if not errorlevel 1 (
    rem -3 accepts newer supported installs; -3.11 requires that exact minor.
    set "ASTRA_CANDIDATE="
    for /f "delims=" %%P in ('py -3 -c "import sys; sys.exit(1) if sys.version_info < (3,11) else None; print(sys._base_executable)" 2^>nul') do set "ASTRA_CANDIDATE=%%P"
    if defined ASTRA_CANDIDATE call :probe_python
)
if defined ASTRA_BOOTSTRAP goto ready
rem An older Python first on PATH must not hide a supported later entry.
for /f "delims=" %%P in ('where python 2^>nul') do (
    if not defined ASTRA_BOOTSTRAP (
        set "ASTRA_CANDIDATE=%%P"
        call :probe_python
    )
)
if defined ASTRA_BOOTSTRAP goto ready
echo Astra: Python 3.11 or newer is required. 1>&2
exit /b 1
:probe_python
rem CALL has no path arguments: do not re-expand percent/bang characters.
rem The extra quotes protect spaced/metacharacter paths in FOR's child CMD.
for /f "delims=" %%P in ('^""%ASTRA_CANDIDATE%" -c "import sys; sys.exit(1) if sys.version_info < (3,11) else None; print(sys._base_executable)" 2^>nul^"') do set "ASTRA_BOOTSTRAP=%%P"
if not defined ASTRA_BOOTSTRAP exit /b 0
"%ASTRA_BOOTSTRAP%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
if errorlevel 1 set "ASTRA_BOOTSTRAP="
exit /b 0
:ready
"%ASTRA_BOOTSTRAP%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
if errorlevel 1 (
    echo Astra: Python 3.11 or newer is required. Selected: "%ASTRA_BOOTSTRAP%" 1>&2
    exit /b 1
)
rem CMD parses the entire final block before Python runs. Updating this file
rem cannot change the commands read when the updater returns. CALL expands only
rem the numeric exit status at execution time; user arguments are not re-expanded.
(
    "%ASTRA_BOOTSTRAP%" "%~dp0astra.py" %*
    call exit /b %%errorlevel%%
)
