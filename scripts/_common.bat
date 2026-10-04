@echo off
rem Fichier partage, appele par les autres scripts avec "call" (ne se lance pas seul).
rem Se place a la racine du projet (config.yaml, .env et data\ y sont relatifs), passe en UTF-8
rem et trouve le Python du venv. PAUSE_AT_END est defini si le script a ete lance par double-clic,
rem pour que la fenetre ne se ferme pas avant que tu aies pu lire.
cd /d "%~dp0.."
set "PYTHONUTF8=1"
chcp 65001 >nul
set "PAUSE_AT_END="
echo %cmdcmdline% | find /i " /c " >nul && set "PAUSE_AT_END=1"
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo Environnement absent : lance d'abord  scripts\setup.bat
  if defined PAUSE_AT_END pause
  exit /b 1
)
exit /b 0
