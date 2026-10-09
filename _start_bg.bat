@echo off
rem CSINS LLM Manager - background launcher
rem 先清掉可能劫持本机回环请求的代理变量，再由 run.py 自身把日志写入 _mgr.log
cd /d "%~dp0"

set HTTP_PROXY=
set HTTPS_PROXY=
set http_proxy=
set https_proxy=
set ALL_PROXY=
set all_proxy=
set NO_PROXY=127.0.0.1,localhost
set no_proxy=127.0.0.1,localhost

rem Python 解释器：优先项目内 .venv，其次环境变量 CSINS_PYTHON，最后 PATH 中的 python
set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if defined CSINS_PYTHON set "PY=%CSINS_PYTHON%"

%PY% run.py --no-browser
