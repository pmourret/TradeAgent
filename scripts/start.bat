@echo off
rem Bot(s) + interface(s) web ensemble, dans cette fenetre. Ctrl+C arrete tout. PAPER TRADING uniquement.
rem
rem   scripts\start.bat               profil hold (la reference, gratuit)
rem   scripts\start.bat llm           le vrai LLM (cle requise, l'API est facturee, plafonnee par le code)
rem   scripts\start.bat hold llm      les deux cote a cote, pour comparer l'agent a "ne rien faire"
rem   scripts\start.bat demo          hors ligne, pour voir l'interface s'animer
rem
rem Options : --no-ui (bots seulement), --no-open (n'ouvre pas le navigateur)
call "%~dp0_common.bat"
if errorlevel 1 exit /b 1

"%PY%" -m tradeagent up %*
set "RC=%ERRORLEVEL%"
if defined PAUSE_AT_END pause
exit /b %RC%
