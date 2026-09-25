@echo off
rem Runs the standalone CTR capture (ctr_warmup.py) with all console output captured to a dated log file.
rem Usage: run_ctr_warmup.bat [ctr_warmup.py args...]   e.g. --dry-run
setlocal
cd /d C:\adwriter

rem Locale-independent, zero-padded timestamp (no spaces, unlike %time% before 10am)
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set STAMP=%%i

if not exist C:\adwriter\ctr_logs mkdir C:\adwriter\ctr_logs
set LOGFILE=C:\adwriter\ctr_logs\ctr_%STAMP%.log

rem UTF-8 so em dashes etc. never crash a redirected print; -u so the log fills as it runs
set PYTHONUTF8=1
echo [run_ctr_warmup] started %STAMP%  args: %* > "%LOGFILE%"
C:\adwriter\adwriter-env\Scripts\python.exe -u C:\adwriter\ctr_warmup.py %* >> "%LOGFILE%" 2>&1
set RC=%ERRORLEVEL%
echo [run_ctr_warmup] exit code %RC% >> "%LOGFILE%"

rem Pass ctr_warmup.py's exit code through so Task Scheduler's "Last Result" is meaningful
endlocal & exit /b %RC%
