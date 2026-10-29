@echo off

rem 仅本地调试用：停掉 dev-preview.bat 起的静态服务器（不影响根目录启动.bat）


chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  停止本地服务器
echo ============================================
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_server.ps1" -Port 8100 %*
echo.
pause
