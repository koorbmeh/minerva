@echo off
rem Python writes text as UTF-8, so non-English characters can't crash it (added 2026-09-28).
set "PYTHONUTF8=1"
title Minerva - Test Venice
cd /d "%~dp0"
echo ==================================================
echo  TEST THE VENICE LANE (low-restriction models)
echo.
echo  This will:
echo   1) list the models your Venice key can use
echo   2) run one tiny governed test chat through
echo      Minerva's gateway (its own sandbox ledger)
echo.
echo  If config\venice.env has no key yet, it just
echo  tells you how to add one and stops.
echo ==================================================
echo.
python scripts\probe_venice.py
echo.
pause
