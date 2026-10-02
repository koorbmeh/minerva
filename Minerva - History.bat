@echo off
rem Python writes text as UTF-8, so non-English characters can't crash it (added 2026-09-28).
set "PYTHONUTF8=1"
title Minerva - history
cd /d "%~dp0"
echo Everything Minerva has pitched or sent so far - a look back.
echo (Nothing gets marked read; Inbox stays as it was.)
echo.
python scripts\host_loop.py --history
pause
