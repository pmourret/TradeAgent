@echo off
rem Le bot en PAPER TRADING (argent fictif), au premier plan, sans interface. Ctrl+C pour arreter.
rem
rem   scripts\paper.bat            profil hold : vrais prix, l'agent ne fait rien (la reference, gratuit)
rem   scripts\paper.bat llm        vrais prix + vrai LLM Anthropic (cle requise, l'API est facturee)
rem   scripts\paper.bat demo       hors ligne : prix simules + agent aleatoire, un cycle toutes les 2 s
rem
rem Les options suivantes sont transmises a "tradeagent run", ex. :  scripts\paper.bat hold --max-cycles 1
rem Chaque profil a sa propre base (data\paper-PROFIL.db) : ils ne se melangent jamais.
call "%~dp0_common.bat"
if errorlevel 1 exit /b 1

set "PROFILE=%~1"
set "EXTRA=%2 %3 %4 %5 %6 %7 %8 %9"
if not defined PROFILE set "PROFILE=hold"
if "%PROFILE:~0,1%"=="-" (
  set "PROFILE=hold"
  set "EXTRA=%*"
)
"%PY%" -m tradeagent run --profile %PROFILE% %EXTRA%
set "RC=%ERRORLEVEL%"
if defined PAUSE_AT_END pause
exit /b %RC%
