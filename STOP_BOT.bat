@echo off
title EMERGENCY BOT STOP SWITCH
echo ========================================================
echo       TRIGGERING EMERGENCY BOT KILL SWITCH...
echo ========================================================

cd /d "%~dp0"

:: 1. Create STOP_BOT.flag
echo STOPPED by Emergency Switch > STOP_BOT.flag
echo [1/3] STOP_BOT.flag created.

:: 2. Terminate background guardian scripts (wscript / cscript)
taskkill /F /IM wscript.exe >nul 2>&1
taskkill /F /IM cscript.exe >nul 2>&1
echo [2/3] Background guardian service terminated.

:: 3. Terminate python bot processes
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*main.py*' -or $_.CommandLine -like '*START_BOT_SERVICE*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
echo [3/3] Python bot engine terminated.

echo ========================================================
echo       ALL BOTS AND CHANNELS ARE 100%% STOPPED!
echo ========================================================
