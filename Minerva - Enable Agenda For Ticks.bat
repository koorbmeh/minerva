@echo off
title Minerva - Enable Agenda For Ticks
cd /d "%~dp0"
echo ==================================================
echo  LET HER BRAINSTORM TICKS WORK HER STANDING AGENDA
echo.
echo  Adds three tools to the daily ticks:
echo   - agenda_list:   see her own task queue
echo   - agenda_add:    queue standing work (max 2/tick,
echo     25 open cap, near-duplicates refused)
echo   - agenda_update: progress notes; 'done' requires
echo     a note saying what actually happened
echo.
echo  The queue is HER OWN file (state\agenda.jsonl) -
echo  none of your mail/calendar content rides the free
echo  tick model because of this. Chat already has these
echo  tools. Backs up the Hermes config first.
echo ==================================================
echo.
set /p X="Type yes to enable (anything else cancels): "
if /i not "%X%"=="yes" ( echo Cancelled - nothing changed. & pause & exit /b 0 )
powershell -ExecutionPolicy Bypass -File scripts\enable_agenda.ps1
echo.
pause
