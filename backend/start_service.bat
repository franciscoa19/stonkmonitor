@echo off
REM StonkMonitor Backend — auto-restart wrapper
REM Logs to backend\logs\service.log
REM Restarts automatically after 10s if the process crashes

cd /d "C:\Users\franc\claude\backend"
if not exist logs mkdir logs

:loop
echo [%date% %time%] Starting StonkMonitor backend... >> logs\service.log
"C:\Users\franc\AppData\Local\Programs\Python\Python313\python.exe" -m uvicorn main:app --host 0.0.0.0 --port 8000 >> logs\service.log 2>&1
echo [%date% %time%] Backend exited (code %ERRORLEVEL%). Restarting in 10s... >> logs\service.log
timeout /t 10 /nobreak >nul
goto loop
