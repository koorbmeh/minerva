@echo off
title Minerva - Enable Connection Awareness
cd /d "%~dp0"
echo ==================================================
echo  LET THE MORNING BRAINSTORM SEE HER CONNECTIONS
echo.
echo  Adds two METADATA-ONLY tools to the daily tick:
echo   - connections_status: what is/is not connected
echo   - request_connection: ask you for one (anti-nag
echo     rules apply - one ask, your answer sticks)
echo.
echo  No email or calendar CONTENT ever rides the free
echo  tick model - actual reading stays on your paid
echo  chat lanes. Backs up the Hermes config first.
echo ==================================================
echo.
set /p X="Type yes to enable (anything else cancels): "
if /i not "%X%"=="yes" ( echo Cancelled - nothing changed. & pause & exit /b 0 )
powershell -ExecutionPolicy Bypass -File scripts\enable_connections.ps1
echo.
pause
