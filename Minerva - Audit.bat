@echo off
rem Python writes text as UTF-8, so non-English characters can't crash it (added 2026-09-28).
set "PYTHONUTF8=1"
title Minerva - Audit (the warden)
cd /d "%~dp0"
echo ==================================================
echo  WARDEN AUDIT - a second, independent model reads
echo  everything Minerva did since the last audit and
echo  tells you plainly if anything looks wrong.
echo  Costs about a cent. Concerns also go to Discord.
echo ==================================================
echo.
python scripts\warden.py
echo.
pause
