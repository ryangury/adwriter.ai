@echo off
rem Runs the daily auto Find candidates search (research.py --auto-candidates) with all console output captured to a dated log file.
rem Usage: run_research_candidates.bat [--dry-run]
setlocal
cd /d C:\adwriter

rem Locale-independent, zero-padded timestamp (no spaces, unlike %time% before 10am)
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set STAMP=%%i

if not exist C:\adwriter\research_logs mkdir C:\adwriter\research_logs
set LOGFILE=C:\adwriter\research_logs\auto_candidates_%STAMP%.log

rem UTF-8 so em dashes etc. never crash a redirected print; -u so the log fills as it runs
set PYTHONUTF8=1
echo [run_research_candidates] started %STAMP%  args: --auto-candidates %*  > "%LOGFILE%"
C:\adwriter\adwriter-env\Scripts\python.exe -u C:\adwriter\research.py --auto-candidates %* >> "%LOGFILE%" 2>&1
set RC=%ERRORLEVEL%
echo [run_research_candidates] exit code %RC% >> "%LOGFILE%"

rem Pass the exit code through so Task Scheduler's "Last Result" is meaningful
endlocal & exit /b %RC%
