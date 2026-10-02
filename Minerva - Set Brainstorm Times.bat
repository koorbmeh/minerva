@echo off
rem Python writes text as UTF-8, so non-English characters can't crash it (added 2026-09-28).
set "PYTHONUTF8=1"
title Minerva - Set Brainstorm Times
cd /d "%~dp0"
echo ==================================================
echo  BRAINSTORM SCHEDULE: morning 06:30 + afternoon 14:30
echo.
echo  Sets the daily tick to 6:30 AM and adds a second
echo  daily tick at 2:30 PM. The laptop must be ON at
echo  those times. Safe to run again.
echo.
echo  Note: the anti-nag rules allow one pitch per
echo  topic per day, so the afternoon tick must find
echo  a DIFFERENT kind of idea than the morning one -
echo  that is by design, not a bug.
echo ==================================================
set /p X="Type yes to apply both (anything else cancels): "
if /i not "%X%"=="yes" ( echo Cancelled - nothing changed. & pause & exit /b 0 )
python -c "import subprocess, sys, os; py = sys.executable; script = os.path.join(os.getcwd(), 'scripts', 'drive_tick.py'); tr = '\"{}\" \"{}\"'.format(py, script); r1 = subprocess.run(['schtasks', '/Create', '/TN', 'Minerva Drive Tick', '/SC', 'DAILY', '/ST', '06:30', '/TR', tr, '/F'], capture_output=True, text=True); print('morning -> 06:30:', (r1.stdout or r1.stderr).strip()); r2 = subprocess.run(['schtasks', '/Create', '/TN', 'Minerva Drive Tick PM', '/SC', 'DAILY', '/ST', '14:30', '/TR', tr, '/F'], capture_output=True, text=True); print('afternoon 14:30:', (r2.stdout or r2.stderr).strip())"
echo.
echo Proof (both tasks should list, 6:30 AM and 2:30 PM):
schtasks /Query /TN "Minerva Drive Tick"
schtasks /Query /TN "Minerva Drive Tick PM"
pause
