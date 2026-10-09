@echo off
chcp 65001 >nul
title CSINS LLM Manager
echo ============================================================
echo    CSINS LLM Manager  v1.0
echo ============================================================
echo    Admin UI : http://127.0.0.1:9090
echo    OpenAI   : http://127.0.0.1:8000/v1
echo    Models   : see config.json  (ai_model_dir)
echo    Engine   : see config.json  (server_exe)
echo    Default password: admin   ^<- change it after first login
echo ============================================================
echo.
cd /d "%~dp0"

set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if defined CSINS_PYTHON set "PY=%CSINS_PYTHON%"

%PY% run.py
pause
