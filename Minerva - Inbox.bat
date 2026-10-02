@echo off
rem Python writes text as UTF-8, so non-English characters can't crash it (added 2026-09-28).
set "PYTHONUTF8=1"
title Minerva - inbox
cd /d "%~dp0"
echo Checking for NEW messages, ideas, and approvals...
echo (Like unread mail: once shown here or texted to you, items
echo  are marked read. To see older items again, double-click
echo  "Minerva - History".)
echo.
python scripts\host_loop.py
pause
