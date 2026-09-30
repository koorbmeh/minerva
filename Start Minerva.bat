@echo off
rem Python writes text as UTF-8, so non-English characters can't crash it (added 2026-09-28).
set "PYTHONUTF8=1"
title Minerva - ON
cd /d "%~dp0"
REM One window runs EVERYTHING (delivery + orchestrator). The supervisor prints
REM its own banner and takes all daemons down when you close this window.
python scripts\supervisor.py
echo.
echo Minerva has stopped. You can close this window.
pause
