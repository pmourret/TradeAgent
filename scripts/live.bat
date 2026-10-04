@echo off
rem MODE REEL : refuse, volontairement. Ce script existe pour que la reponse soit claire et au meme endroit.
rem Le code ne sait pas passer d'ordre sur un vrai compte : voir le README, "Chemin vers l'argent reel".
cd /d "%~dp0.."
set "PYTHONUTF8=1"
chcp 65001 >nul
set "PAUSE_AT_END="
echo %cmdcmdline% | find /i " /c " >nul && set "PAUSE_AT_END=1"

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m tradeagent run --profile live
) else (
  echo Le mode reel n'existe pas encore dans ce projet, voir README : "Chemin vers l'argent reel".
)
if defined PAUSE_AT_END pause
exit /b 2
