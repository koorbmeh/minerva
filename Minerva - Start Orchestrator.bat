@echo off
rem Python writes text as UTF-8, so non-English characters can't crash it (added 2026-09-28).
set "PYTHONUTF8=1"
title Minerva - Orchestrator (standalone)
cd /d "%~dp0"
echo ==================================================
echo  ORCHESTRATOR (standalone / optional)
echo.
echo  You normally do NOT need this. "Start Minerva" already
echo  runs the orchestrator together with everything else.
echo  Use this ONLY to run the background-job worker by itself.
echo  If one is already running, this will say so and close.
echo ==================================================
python scripts\run_orchestrator.py --watch
pause
