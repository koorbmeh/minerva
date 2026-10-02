@echo off
rem Python writes text as UTF-8, so non-English characters can't crash it (added 2026-09-28).
set "PYTHONUTF8=1"
title Minerva - Connections
cd /d "%~dp0"
echo ==================================================
echo  MINERVA'S CONNECTIONS (email, calendar, ...)
echo.
echo  Shows what Minerva is connected to. For anything
echo  CONNECTED it runs one real read-only test through
echo  the governed gateway (results print only here).
echo  For anything NOT connected it prints the exact
echo  steps to enable it - your call, no pressure.
echo ==================================================
echo.
python scripts\probe_connections.py
echo.
pause
