@echo off
rem Python writes text as UTF-8, so non-English characters can't crash it (added 2026-09-28).
set "PYTHONUTF8=1"
title Minerva - brainstorming...
cd /d "%~dp0"
echo Asking Minerva to brainstorm ONE new idea for you.
echo (Skip this if you are mid-conversation with Hermes right now.)
echo.
python scripts\drive_tick.py
echo.
echo Done. If she pitched an idea it will arrive as a text when
echo "Start Minerva" is running - or double-click "Minerva - Inbox".
pause
