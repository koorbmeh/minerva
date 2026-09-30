@echo off
rem Python writes text as UTF-8, so non-English characters can't crash it (added 2026-09-28).
set "PYTHONUTF8=1"
title Minerva - Resume
cd /d "%~dp0"
echo ==================================================
echo  RESUME MINERVA (clears the kill switch)
echo ==================================================
set /p X="Type yes to clear the STOP and let her run again: "
if /i not "%X%"=="yes" ( echo Still stopped - nothing changed. & pause & exit /b 0 )
python -c "import sys; sys.path.insert(0,'src'); from minerva_core.killswitch import resume; print('RESUMED - she can run again.' if resume('state') else 'There was no STOP file - she was not stopped.')"
echo.
pause
