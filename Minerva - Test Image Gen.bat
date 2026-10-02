@echo off
rem Python writes text as UTF-8, so non-English characters can't crash it (added 2026-09-28).
set "PYTHONUTF8=1"
title Minerva - Test Image Gen
cd /d "%~dp0"
echo ==================================================
echo  TEST IMAGE GENERATION (Venice lane)
echo.
echo  This will:
echo   1) list the image + video models your key can use
echo   2) generate ONE small test image through Minerva's
echo      governed gateway (its own sandbox ledger)
echo      and show you the file path to open it
echo.
echo  Cost: a few cents. If config\venice.env has no key
echo  yet, it just tells you how to add one and stops.
echo ==================================================
echo.
python scripts\probe_generation.py
echo.
pause
