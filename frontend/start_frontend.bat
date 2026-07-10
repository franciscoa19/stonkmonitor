@echo off
REM StonkMonitor Frontend — auto-restart wrapper
REM Logs to frontend\logs\frontend.log
REM Restarts automatically after 10s if the dev server crashes.
REM Idempotent: if port 3000 is already serving, it idles instead of
REM starting a second copy — safe to launch from multiple triggers.

cd /d "C:\Users\franc\claude\frontend"
if not exist logs mkdir logs

:loop
netstat -ano | findstr /R /C:":3000 .*LISTENING" >nul
if %ERRORLEVEL%==0 (
  timeout /t 30 /nobreak >nul
  goto loop
)
echo [%date% %time%] Starting StonkMonitor frontend... >> logs\frontend.log
call npm.cmd run dev >> logs\frontend.log 2>&1
echo [%date% %time%] Frontend exited (code %ERRORLEVEL%). Restarting in 10s... >> logs\frontend.log
timeout /t 10 /nobreak >nul
goto loop
