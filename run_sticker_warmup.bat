@echo off
rem Runs the non-MB window sticker warmup (sticker_warmup.py --skip-predictive) with all console output captured to a dated log file.
rem Usage: run_sticker_warmup.bat [sticker_warmup args...]   e.g. --dry-run
setlocal
cd /d C:\adwriter

rem Locale-independent, zero-padded timestamp (no spaces, unlike %time% before 10am)
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set STAMP=%%i

if not exist C:\adwriter\sticker_logs mkdir C:\adwriter\sticker_logs
set LOGFILE=C:\adwriter\sticker_logs\sticker_warmup_%STAMP%.log

rem UTF-8 so em dashes etc. never crash a redirected print; -u so the log fills as it runs
set PYTHONUTF8=1
echo [run_sticker_warmup] started %STAMP%  args: --skip-predictive %*  > "%LOGFILE%"
C:\adwriter\adwriter-env\Scripts\python.exe -u C:\adwriter\sticker_warmup.py --skip-predictive %* >> "%LOGFILE%" 2>&1
set RC=%ERRORLEVEL%
echo [run_sticker_warmup] exit code %RC% >> "%LOGFILE%"

rem Pass the warmup's exit code through so Task Scheduler's "Last Result" is meaningful
endlocal & exit /b %RC%
