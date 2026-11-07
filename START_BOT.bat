@echo off
title START BOT SERVICE
echo ========================================================
echo       STARTING BINANCE EMA BOT SERVICE...
echo ========================================================

cd /d "%~dp0"

:: Remove stop flag if present
if exist STOP_BOT.flag (
    del /f /q STOP_BOT.flag
    echo Removed STOP_BOT.flag lock.
)

:: Launch background service silently via VBS
start "" wscript.exe "%~dp0START_BOT_SERVICE.vbs"

echo ========================================================
echo       BOT SERVICE LAUNCHED IN BACKGROUND!
echo ========================================================
