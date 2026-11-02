@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem Banner and the "using python" line are printed by server (__main__.py).
rem Start scripts may lag behind (older updaters skipped them), so printing
rem from Python keeps every install looking the same.

rem ---- 1) package completeness ------------------------------------------
rem Three common failures used to surface as just:
rem   "python.exe: No module named server"
rem which tells the user nothing. Check the layout first.
rem   1) copied only the .bat out, without the folder
rem   2) double-clicked it inside the zip (Explorer extracts a single file)
rem   3) extraction was interrupted
rem ASCII-only on purpose: see the header of tools/make_start_bat.py.
if not exist ".\server\__init__.py" goto no_server
if not exist ".\web\index.html" goto no_server

rem ---- 2) pick python: bundled first, then system -----------------------
set "PYEXE="
set "PYARGS="
set "BUILTIN="
if exist "%~dp0python\python.exe" goto use_builtin

where py >nul 2>nul
if not errorlevel 1 goto use_py
where python >nul 2>nul
if not errorlevel 1 goto use_python
goto no_python

:use_builtin
set "PYEXE=%~dp0python\python.exe"
set "BUILTIN=1"
goto have_python

:use_py
set "PYEXE=py"
set "PYARGS=-3"
goto have_python

:use_python
set "PYEXE=python"
goto have_python

:have_python
rem server prints the banner / python path; nothing to echo here.

rem ---- 3) deps: only needed for system python ---------------------------
if defined BUILTIN goto launch
%PYEXE% %PYARGS% -c "import UnityPy, lz4, PIL, numpy" >nul 2>nul
if not errorlevel 1 goto launch
call :say dep_missing
%PYEXE% %PYARGS% "%~dp0tools\bootstrap_deps.py"
echo.

rem ---- 4) launch -------------------------------------------------------
:launch
set "PYTHONPATH=%~dp0"
rem 默认监听 0.0.0.0（局域网），手机/同网段设备可访问；本机仍可用 127.0.0.1
"%PYEXE%" %PYARGS% -m server --host 0.0.0.0 --open
echo.
echo [提示] 若手机连不上，请放行 Windows 防火墙入站端口（管理员 PowerShell 执行）：
echo   New-NetFirewallRule -DisplayName "ESCAH LAN" -Direction Inbound -LocalPort ^<端口^> -Protocol TCP -Action Allow
echo   端口见上方启动日志打印的值
if not errorlevel 1 goto done
echo.
call :say launch_fail
echo   "%PYEXE%" %PYARGS% -m server --open
echo.
pause
goto done

:no_server
call :say no_server
echo   %CD%
echo.
pause
goto done

:no_python
call :say no_python
pause
goto done

:done
endlocal
exit /b 0

rem ---- helper: print a UTF-8 message file -------------------------------
rem Falls back to an ASCII version when tools/msg is missing too
rem (e.g. the user copied *only* the .bat -- the very case we diagnose).
rem Missing banner/using/... stay silent: those are cosmetic, and a warning
rem about them would only add noise on top of the real message.
:say
if "%~1"=="" goto :eof
if exist "%~dp0tools\msg\%~1.txt" goto say_type
if /i "%~1"=="no_server" goto fb_no_server
if /i "%~1"=="no_python" goto fb_no_python
goto :eof

:say_type
type "%~dp0tools\msg\%~1.txt"
goto :eof

:fb_no_server
echo [x] Incomplete folder: no "server" directory next to this file.
echo.
echo     Causes: copied only the .bat / ran it inside the zip /
echo             extraction was interrupted.
echo     Fix: extract the WHOLE zip to a folder, then run this file there.
echo.
goto :eof

:fb_no_python
echo [x] Python not found. The portable package should ship python\python.exe
echo     next to this file -- its absence means the package is incomplete.
echo     Re-download ESCAH-Offline-v*-win64-portable.zip and extract it fully.
echo.
goto :eof
