@echo off
rem Python writes text as UTF-8, so non-English characters can't crash it (added 2026-09-28).
set "PYTHONUTF8=1"
title Minerva - Agenda
cd /d "%~dp0"
python scripts\show_agenda.py
echo.
pause
