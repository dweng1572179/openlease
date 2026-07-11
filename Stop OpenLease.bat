@echo off
rem Stops ONLY the OpenLease process — scoped by port, not by image name. OpenProp runs the
rem identical uvicorn.exe module path, so `taskkill /im uvicorn.exe` would kill both; port 8788
rem is what distinguishes OpenLease (OpenProp holds 8787), so we find the PID bound to that
rem port and kill only it.
if not defined OPENLEASE_PORT set OPENLEASE_PORT=8788
set FOUND=0
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%OPENLEASE_PORT% " ^| findstr "LISTENING"') do (
  taskkill /f /pid %%p >nul 2>nul
  set FOUND=1
)
if "%FOUND%"=="1" (
  echo OpenLease (port %OPENLEASE_PORT%) stopped.
) else (
  echo OpenLease (port %OPENLEASE_PORT%) was not running.
)
echo You can close this window.
pause
