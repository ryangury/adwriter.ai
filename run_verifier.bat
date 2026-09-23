@echo off
rem Runs the standalone verifier (verifier.py --all) with all console output captured to a dated log file.
rem Usage: run_verifier.bat [verifier args...]   e.g. --no-email
setlocal
cd /d C:\adwriter

rem Locale-independent, zero-padded timestamp (no spaces, unlike %time% before 10am)
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set STAMP=%%i

if not exist C:\adwriter\verifier_logs mkdir C:\adwriter\verifier_logs
set LOGFILE=C:\adwriter\verifier_logs\verifier_%STAMP%.log

rem UTF-8 so em dashes etc. never crash a redirected print; -u so the log fills as it runs
set PYTHONUTF8=1
echo [run_verifier] started %STAMP%  args: --all --no-email %*  > "%LOGFILE%"
C:\adwriter\adwriter-env\Scripts\python.exe -u C:\adwriter\verifier.py --all --no-email %* >> "%LOGFILE%" 2>&1
set RC=%ERRORLEVEL%
echo [run_verifier] exit code %RC% >> "%LOGFILE%"

rem Pass the verifier's exit code through so Task Scheduler's "Last Result" is meaningful
endlocal & exit /b %RC%
