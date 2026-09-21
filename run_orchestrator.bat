@echo off
rem Runs the orchestrator with all console output captured to a dated log file.
rem Usage: run_orchestrator.bat [orchestrator args...]   e.g. --status 11 12 16 --limit 1 --no-email
setlocal
cd /d C:\adwriter

rem Locale-independent, zero-padded timestamp (no spaces, unlike %time% before 10am)
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set STAMP=%%i

if not exist C:\adwriter\orchestrator_logs mkdir C:\adwriter\orchestrator_logs
set LOGFILE=C:\adwriter\orchestrator_logs\orchestrator_%STAMP%.log

rem UTF-8 so em dashes etc. never crash a redirected print; -u so the log fills as it runs
set PYTHONUTF8=1
echo [run_orchestrator] started %STAMP%  args: %*  > "%LOGFILE%"
C:\adwriter\adwriter-env\Scripts\python.exe -u C:\adwriter\orchestrator.py %* >> "%LOGFILE%" 2>&1
set RC=%ERRORLEVEL%
echo [run_orchestrator] exit code %RC% >> "%LOGFILE%"

rem Pass the orchestrator's exit code through so Task Scheduler's "Last Result" is meaningful
endlocal & exit /b %RC%
