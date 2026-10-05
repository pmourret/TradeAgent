@echo off
rem L'interface web seule (lecture seule, locale), pour regarder un bot lance ailleurs. Ctrl+C pour arreter.
rem
rem   scripts\ui.bat [hold|llm|demo|board]      ports : hold 8765, llm 8766, demo 8767, board 8768
rem
rem Pour lancer bot ET interface d'un coup : scripts\start.bat
call "%~dp0_common.bat"
if errorlevel 1 exit /b 1

set "PROFILE=%~1"
set "EXTRA=%2 %3 %4 %5 %6 %7 %8 %9"
if not defined PROFILE set "PROFILE=hold"
if "%PROFILE:~0,1%"=="-" (
  set "PROFILE=hold"
  set "EXTRA=%*"
)
"%PY%" -m tradeagent web --profile %PROFILE% --open %EXTRA%
set "RC=%ERRORLEVEL%"
if defined PAUSE_AT_END pause
exit /b %RC%
