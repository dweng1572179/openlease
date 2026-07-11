@echo off
taskkill /f /im uvicorn.exe >nul 2>nul
echo OpenLease stopped. You can close this window.
pause
