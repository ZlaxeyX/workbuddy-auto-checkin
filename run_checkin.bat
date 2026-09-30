@echo off
rem ============================================================
rem  WorkBuddy daily credit auto-checkin - manual runner
rem  Double-click to run once, or pass checkin.py arguments:
rem      run_checkin.bat --show-status
rem      run_checkin.bat --mode client --force
rem  Set WB_NO_PAUSE=1 to skip the "press any key" prompt.
rem ============================================================
setlocal
cd /d "%~dp0"

set "PY="

rem 1) bundled python shipped with WorkBuddy
for /d %%d in ("%USERPROFILE%\.workbuddy\binaries\python\versions\*") do (
    if not defined PY (
        if exist "%%~fd\python.exe" set "PY=%%~fd\python.exe"
    )
)

rem 2) python on PATH
if not defined PY (
    for /f "delims=" %%p in ('where python.exe 2^>nul') do (
        if not defined PY set "PY=%%p"
    )
)

rem 3) py launcher
if not defined PY (
    for /f "delims=" %%p in ('where py.exe 2^>nul') do (
        if not defined PY set "PY=%%p -3"
    )
)

if not defined PY (
    echo [ERROR] Python not found. Install Python 3.9+ and retry.
    echo         Or register the scheduled task with: -PythonPath "C:\path\to\python.exe"
    if not defined WB_NO_PAUSE pause
    exit /b 5
)

echo Using Python: %PY%
echo.

%PY% "%~dp0checkin.py" %*
set "RC=%ERRORLEVEL%"

echo.
echo Exit code: %RC%
if "%RC%"=="0" echo   0  today's credits are in place / already checked in
if "%RC%"=="2" echo   2  client online, but one click on the checkin bubble is needed
if "%RC%"=="3" echo   3  auth expired - sign in again or refresh access_token
if "%RC%"=="4" echo   4  network / server error, retries exhausted
if "%RC%"=="5" echo   5  environment problem (client not found etc.)

if not defined WB_NO_PAUSE pause
endlocal & exit /b %RC%
