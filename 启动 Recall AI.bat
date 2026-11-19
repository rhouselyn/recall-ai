@echo off
setlocal
cd /d "%~dp0"
set "RECALL_PY=.venv\Scripts\python.exe"
if exist "%RECALL_PY%" goto dependencies

echo Creating the Recall AI environment...
py -3.11 -m venv .venv 2>nul
if not errorlevel 1 goto dependencies
py -3 -c "import sys; sys.exit(sys.version_info < (3, 11))" 2>nul
if not errorlevel 1 goto launcher
python -c "import sys; sys.exit(sys.version_info < (3, 11))" 2>nul
if errorlevel 1 goto python_missing
python -m venv .venv || goto failed
goto dependencies

:launcher
py -3 -m venv .venv || goto failed

:dependencies
fc /b pyproject.toml ".venv\recall-installed.toml" >nul 2>&1
if not errorlevel 1 goto launch
echo Installing Recall AI and its web dependencies...
"%RECALL_PY%" -m pip --version >nul 2>&1
if errorlevel 1 "%RECALL_PY%" -m ensurepip --upgrade || goto failed
"%RECALL_PY%" -m pip install --disable-pip-version-check -e ".[browser,archive,semantic,mcp]" || goto failed
copy /y pyproject.toml ".venv\recall-installed.toml" >nul || goto failed

:launch
"%RECALL_PY%" -m recall serve --open || goto failed
exit /b 0

:python_missing
echo Install Python 3.11 or newer from https://www.python.org/downloads/ and try again.
pause
exit /b 1

:failed
echo Recall AI could not start. Check the error above and run this file again.
pause
exit /b 1
