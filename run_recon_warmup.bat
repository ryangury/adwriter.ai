@echo off
rem Runs the recon cache warmup (recon_warmup.py) with all console output captured to a dated log file.
rem Usage: run_recon_warmup.bat [recon_warmup args...]   e.g. --limit 5
setlocal
cd /d C:\adwriter

rem Locale-independent, zero-padded timestamp (no spaces, unlike %time% before 10am)
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set STAMP=%%i

if not exist C:\adwriter\recon_logs mkdir C:\adwriter\recon_logs
set LOGFILE=C:\adwriter\recon_logs\recon_warmup_%STAMP%.log

rem UTF-8 so em dashes etc. never crash a redirected print; -u so the log fills as it runs
set PYTHONUTF8=1
echo [run_recon_warmup] started %STAMP%  args: %*  > "%LOGFILE%"
C:\adwriter\adwriter-env\Scripts\python.exe -u C:\adwriter\recon_warmup.py %* >> "%LOGFILE%" 2>&1
set RC=%ERRORLEVEL%
echo [run_recon_warmup] exit code %RC% >> "%LOGFILE%"

rem Pass the warmup's exit code through so Task Scheduler's "Last Result" is meaningful
endlocal & exit /b %RC%
