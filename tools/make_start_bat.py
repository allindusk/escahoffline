# -*- coding: utf-8 -*-
"""生成根目录的 ``启动.bat``。

为什么要有这个脚本
------------------
两个坑，逼出这个方案：

1. **cmd 解析带中文的批处理会丢行。** 实测同一份脚本存成 UTF-8（无 BOM）时，
   ``:no_server`` 那段会少掉两三行 echo，用户看到的是残缺提示；加 BOM 又会让
   ``@echo off`` 失效（BOM 成了命令的一部分）。
2. 存成 **GBK** 虽然 cmd 认，但编辑器 / git diff 里全是乱码，没法维护。

所以 ``启动.bat`` 本身**纯 ASCII**（注释也必须是英文，否则照样会丢行），
中文提示放在 ``tools/msg/*.txt``（UTF-8），运行时 ``chcp 65001`` + ``type`` 打出来。

- 改提示文案 → 改 ``tools/msg/*.txt``
- 改流程逻辑 → 改本脚本后重跑 ``python tools/make_start_bat.py``

脚本末尾会自检：bat 里只要混进一个非 ASCII 字符就报错退出。
"""
from __future__ import annotations

import io
import os
import sys

BAT = """@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

call :say banner
echo.

rem ---- 1) package completeness ------------------------------------------
rem Three common failures used to surface as just:
rem   "python.exe: No module named server"
rem which tells the user nothing. Check the layout first.
rem   1) copied only the .bat out, without the folder
rem   2) double-clicked it inside the zip (Explorer extracts a single file)
rem   3) extraction was interrupted
rem ASCII-only on purpose: see the header of tools/make_start_bat.py.
if not exist ".\\server\\__init__.py" goto no_server
if not exist ".\\web\\index.html" goto no_server

rem ---- 2) pick python: bundled first, then system -----------------------
set "PYEXE="
set "PYARGS="
set "BUILTIN="
if exist "%~dp0python\\python.exe" goto use_builtin

where py >nul 2>nul
if not errorlevel 1 goto use_py
where python >nul 2>nul
if not errorlevel 1 goto use_python
goto no_python

:use_builtin
set "PYEXE=%~dp0python\\python.exe"
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
call :say using
echo   %PYEXE% %PYARGS%
echo.

rem ---- 3) deps: only needed for system python ---------------------------
if defined BUILTIN goto launch
%PYEXE% %PYARGS% -c "import UnityPy, lz4, PIL, numpy" >nul 2>nul
if not errorlevel 1 goto launch
call :say dep_missing
%PYEXE% %PYARGS% "%~dp0tools\\bootstrap_deps.py"
echo.

rem ---- 4) launch -------------------------------------------------------
:launch
set "PYTHONPATH=%~dp0"
"%PYEXE%" %PYARGS% -m server --open
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
if exist "%~dp0tools\\msg\\%~1.txt" goto say_type
if /i "%~1"=="no_server" goto fb_no_server
if /i "%~1"=="no_python" goto fb_no_python
goto :eof

:say_type
type "%~dp0tools\\msg\\%~1.txt"
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
echo [x] Python not found. The portable package should ship python\\python.exe
echo     next to this file -- its absence means the package is incomplete.
echo     Re-download ESCAH-Offline-v*-win64-portable.zip and extract it fully.
echo.
goto :eof
"""

MSG_FILES = ("banner", "using", "no_server", "no_python",
             "dep_missing", "launch_fail")


def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dst = os.path.join(root, "启动.bat")
    body = BAT.replace("\r\n", "\n").replace("\n", "\r\n")

    bad = [(i + 1, ln) for i, ln in enumerate(body.split("\r\n"))
           if any(ord(c) > 127 for c in ln)]
    if bad:
        for i, ln in bad:
            print(f"!! 第 {i} 行含非 ASCII，cmd 可能丢行：{ln[:70]}",
                  file=sys.stderr)
        return 1

    raw = body.encode("ascii")
    with open(dst, "wb") as f:
        f.write(raw)
    print(f"已生成 {dst}（ASCII / CRLF，{len(raw)} 字节，"
          f"{len(body.splitlines())} 行）")

    ok = True
    for name in MSG_FILES:
        p = os.path.join(root, "tools", "msg", name + ".txt")
        if not os.path.exists(p):
            print(f"!! 缺少提示文件 {p}", file=sys.stderr)
            ok = False
            continue
        if io.open(p, "rb").read().startswith(b"\xef\xbb\xbf"):
            print(f"!! {name}.txt 带 BOM，type 出来会多出乱码", file=sys.stderr)
            ok = False
    if ok:
        print("提示文件齐全且无 BOM")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
