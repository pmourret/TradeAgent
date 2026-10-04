@echo off
rem L'etat d'un profil (ou de tous ceux qui ont deja tourne) : equity, resultat net apres cout de l'API, soldes.
rem
rem   scripts\status.bat           tous les profils qui ont deja tourne
rem   scripts\status.bat llm       un seul
call "%~dp0_common.bat"
if errorlevel 1 exit /b 1

if "%~1"=="" (
  "%PY%" -m tradeagent status --all
) else (
  "%PY%" -m tradeagent status --profile %~1
)
set "RC=%ERRORLEVEL%"
if defined PAUSE_AT_END pause
exit /b %RC%
