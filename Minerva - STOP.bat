@echo off
rem Python writes text as UTF-8, so non-English characters can't crash it (added 2026-09-28).
set "PYTHONUTF8=1"
title Minerva - STOP
cd /d "%~dp0"
echo ==================================================
echo  EMERGENCY STOP
echo  Halts EVERYTHING immediately: every tool call is
echo  denied, no brainstorm ticks run, chat answers
echo  "I'm stopped" without spending a cent.
echo ==================================================
set /p R="Why are you stopping her? (optional note): "
python -c "import sys; sys.path.insert(0,'src'); from minerva_core.killswitch import stop; p=stop('state', r'''%R%'''); print('STOPPED. The switch is the file: ' + p); print('Resume with: Minerva - Resume.bat (or delete that file).')"
echo.
pause
