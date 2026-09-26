@echo off
REM ============================================================
REM BB incremental update - started by the Windows task BB_WeeklyUpdate
REM (Saturday and Sunday 20:00; see the scheduled-task section of the README)
REM
REM Downloads only content that is new or changed. Writes a report even when
REM there is nothing new.
REM Exit codes: 0=ok 1=unexpected error 2=needs a human to log in
REM             3=something was found but not retrieved 4=already running
REM
REM Comments here are ASCII on purpose: cmd.exe reads this file in the OEM code
REM page, so non-ASCII comments are mis-decoded and the fragments get executed
REM as commands. Keep this file ASCII.
REM ============================================================
cd /d "%~dp0.."

REM Whatever `python` resolves to on PATH. Point this at an absolute interpreter
REM path if the machine has more than one.
set PY=python
set LOG=logs\update.log
if not exist logs mkdir logs

echo ===== %date% %time% START ===== >> "%LOG%"
"%PY%" bb_crawler.py --update >> "%LOG%" 2>&1
set RC=%ERRORLEVEL%
echo ===== %date% %time% DONE rc=%RC% ===== >> "%LOG%"

exit /b %RC%
