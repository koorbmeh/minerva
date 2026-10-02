@echo off
rem Python writes text as UTF-8, so non-English characters can't crash it (added 2026-09-28).
set "PYTHONUTF8=1"
title Minerva - Enable Morning Brief
cd /d "%~dp0"
echo ==================================================
echo  MORNING BRIEF: a daily 06:50 email briefing by DM
echo.
echo  Registers a daily 06:50 task: Minerva reads your
echo  unread email (read-only) and calendar (when you
echo  connect one), then DMs you ONE short brief - a job
echo  Minerva pitched herself.
echo.
echo  Runs on your paid Anthropic key (about a cent a
echo  day, metered to the spend log; the free model
echo  never sees your mail). Nothing connected = it
echo  skips silently, free. The laptop must be ON at
echo  06:50. Safe to run again.
echo ==================================================
set /p X="Type yes to register it (anything else cancels): "
if /i not "%X%"=="yes" ( echo Cancelled - nothing changed. & pause & exit /b 0 )
python -c "import subprocess, sys, os; py = sys.executable; script = os.path.join(os.getcwd(), 'scripts', 'morning_brief.py'); tr = '\"{}\" \"{}\"'.format(py, script); r = subprocess.run(['schtasks', '/Create', '/TN', 'Minerva Morning Brief', '/SC', 'DAILY', '/ST', '06:50', '/TR', tr, '/F'], capture_output=True, text=True); print('morning brief 06:50:', (r.stdout or r.stderr).strip())"
echo.
echo Proof (should list at 6:50 AM):
schtasks /Query /TN "Minerva Morning Brief"
pause
