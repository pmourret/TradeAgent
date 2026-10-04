@echo off
rem Installation (a faire une fois) : environnement Python isole (.venv), dependances, fichier .env,
rem puis verification par les tests (hors ligne, quelques secondes).
cd /d "%~dp0.."
set "PYTHONUTF8=1"
chcp 65001 >nul
set "PAUSE_AT_END="
echo %cmdcmdline% | find /i " /c " >nul && set "PAUSE_AT_END=1"

set "PYEXE="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "PYEXE=py -3"
if not defined PYEXE python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "PYEXE=python"
if not defined PYEXE (
  echo Python 3.10 ou plus introuvable. Installe-le depuis https://www.python.org/downloads/
  echo et coche "Add python.exe to PATH" pendant l'installation, puis relance ce script.
  if defined PAUSE_AT_END pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creation de l'environnement .venv...
  %PYEXE% -m venv .venv
  if errorlevel 1 goto :fail
)
echo Installation des dependances...
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".venv\Scripts\python.exe" -m pip install --quiet -e ".[dev]"
if errorlevel 1 goto :fail

if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo Fichier .env cree. Ta cle Anthropic n'y est necessaire que pour le profil llm.
)

echo Verification, tests hors ligne...
".venv\Scripts\python.exe" -m pytest -q
if errorlevel 1 goto :fail

echo.
echo Pret. Pour essayer tout de suite, sans cle ni reseau :   scripts\start.bat demo
if defined PAUSE_AT_END pause
exit /b 0

:fail
echo.
echo L'installation a echoue, voir les messages ci-dessus.
if defined PAUSE_AT_END pause
exit /b 1
