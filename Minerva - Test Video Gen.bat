@echo off
rem Python writes text as UTF-8, so non-English characters can't crash it (added 2026-09-28).
set "PYTHONUTF8=1"
title Minerva - Test Video Gen
cd /d "%~dp0"
echo ==================================================
echo  TEST VIDEO GENERATION (Venice lane) - REAL MONEY
echo.
echo  This runs the image test first (cents), then shows
echo  you the PRICE QUOTE for one short test video and
echo  asks before spending anything on it. Expect the
echo  video to take a few minutes once you say yes.
echo ==================================================
echo.
python scripts\probe_generation.py --video
echo.
pause
