@echo off
rem ============================================================
rem  CSINS LLM Manager - restart manager + gateway
rem  Applies changes in app/*.py and static/*.html
rem  Keeps llama-server processes alive (no model reload).
rem
rem  NOTE: after restart NO model is running (autostart=false for
rem        all models). The gateway now loads a model ON DEMAND:
rem        the first /v1 request for an alias will cold-start it
rem        (and will refuse to switch if another model is in use).
rem
rem  Log: _restart_service.log (same folder as this script)
rem ============================================================
setlocal
cd /d "%~dp0"

call :run >> "_restart_service.log" 2>&1

echo.
echo Done.  Admin UI : http://127.0.0.1:9090
echo        Gateway  : http://127.0.0.1:8000/v1
echo.
echo Log saved to _restart_service.log
echo Next: just call the gateway - the model loads on demand.
endlocal
exit /b 0

:run
echo .
echo ===== %date% %time% restart invoked =====

echo [1/4] Stopping old run.py processes (llama-server is kept) ...
powershell -NoProfile -Command "$ps = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*run.py*' }; if (-not $ps) { Write-Host '  (none running)' }; foreach ($p in $ps) { Write-Host ('  kill PID ' + $p.ProcessId); Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }; foreach ($p in $ps) { try { Wait-Process -Id $p.ProcessId -Timeout 15 -ErrorAction SilentlyContinue } catch {} }; Start-Sleep -Seconds 2"

echo [2/4] Waiting for ports 8000/9090 to be released ...
ping -n 4 127.0.0.1 >nul

echo [3/4] Starting fresh instance (console log: _mgr.log) ...
start "CSINS-LLM" /min cmd /c "call _start_bg.bat"

echo [4/4] Health check (waiting up to 40s) ...
set /a _try=0
:wait
set /a _try+=1
curl.exe -s --noproxy "*" -m 3 http://127.0.0.1:8000/health > "%TEMP%\_csins_h.txt" 2>nul
findstr /c:"ok" "%TEMP%\_csins_h.txt" >nul 2>&1
if not errorlevel 1 goto :ready
if %_try% geq 13 goto :timeout
ping -n 4 127.0.0.1 >nul
goto :wait

:ready
echo   gateway 8000 : UP
curl.exe -s -o nul -w "  admin 9090   : HTTP %%{http_code}\n" --noproxy "*" -m 5 http://127.0.0.1:9090/
echo   --- listening ports ---
netstat -ano | findstr /r /c:":8000 .*LISTENING" /c:":9090 .*LISTENING" /c:":1808[0-9] .*LISTENING"
goto :eof

:timeout
echo   !! gateway 8000 did NOT come up within 40s - check _mgr.log
goto :eof
