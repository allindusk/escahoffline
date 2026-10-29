@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================
echo   超昂大战 角色剧情离线阅读器
echo ============================================
echo.

rem ---- 找 Python：优先 py 启动器，其次 python ----
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo [x] 没有找到 Python。
  echo.
  echo     请安装 Python 3.8 或更新版本： https://www.python.org/downloads/
  echo     安装时记得勾选 "Add Python to PATH"。
  echo.
  pause
  exit /b 1
)

rem ---- 依赖：缺就用 bootstrap 安装（会先测速选最快镜像，带下载进度） ----
rem 那几个包合计约 40 MB，直连 PyPI 官方源常常只有几十 KB/s，
rem 所以先实测清华/阿里/腾讯等镜像，挑最快的那条再下。
%PY% -c "import UnityPy, lz4, PIL, numpy" >nul 2>nul
if errorlevel 1 (
  echo [!] 还缺运行依赖（UnityPy / lz4 / Pillow / numpy），
  echo     没有它们无法从官方 CDN 解包资源。
  echo.
  %PY% "%~dp0tools\bootstrap_deps.py"
  echo.
)

rem ---- 启动 ----
%PY% -m server --open
if errorlevel 1 (
  echo.
  echo [x] 启动失败。可以手动运行： python -m server --open
  pause
)
endlocal
