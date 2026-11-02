@echo off

rem 仅本地调试用：停掉 dev-preview.bat 起的服务（不影响根目录启动.bat）
rem 按端口 8100 停止进程，与监听地址（127.0.0.1 或 0.0.0.0）无关，无需修改。


chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  停止本地服务器
echo ============================================
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_server.ps1" -Port 8100 %*
echo.
pause
