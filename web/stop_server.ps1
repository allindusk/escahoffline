# 关闭本地静态服务器（启动.bat / serve.py 起的那个）
#
# 只动两类进程，绝不误杀其它 python（比如跑批脚本）：
#   1) 正在监听目标端口的进程
#   2) 命令行里含 serve.py 或 http.server 的 python 进程
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File stop_server.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File stop_server.ps1 -Port 8012
param([int]$Port = 8000)

$ErrorActionPreference = 'SilentlyContinue'
$targets = New-Object System.Collections.Generic.HashSet[int]

foreach ($conn in Get-NetTCPConnection -State Listen -LocalPort $Port) {
    if ($conn.OwningProcess -gt 0) { [void]$targets.Add([int]$conn.OwningProcess) }
}

Get-CimInstance Win32_Process -Filter "Name='python.exe' or Name='pythonw.exe'" | ForEach-Object {
    if ($_.CommandLine -and ($_.CommandLine -match 'serve\.py' -or $_.CommandLine -match 'http\.server')) {
        [void]$targets.Add([int]$_.ProcessId)
    }
}

if ($targets.Count -eq 0) {
    Write-Host "没有发现正在运行的服务（端口 $Port 是空闲的）" -ForegroundColor DarkGray
    exit 0
}

$killed = 0
$failed = 0
foreach ($procId in $targets) {
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$procId"
    if (-not $p) { continue }
    $desc = "PID $procId  $($p.Name)"
    try {
        Stop-Process -Id $procId -Force -ErrorAction Stop
        Write-Host "  已关闭 $desc" -ForegroundColor Green
        $killed++
    } catch {
        Write-Host "  关闭失败 $desc —— $($_.Exception.Message)" -ForegroundColor Red
        $failed++
    }
}

Start-Sleep -Milliseconds 500
$left = @(Get-NetTCPConnection -State Listen -LocalPort $Port)
if ($left.Count -gt 0) {
    Write-Host "端口 $Port 仍被 PID $($left[0].OwningProcess) 占用（可能不是本账号启动的，需要用管理员身份再试）" -ForegroundColor Yellow
    exit 1
}

Write-Host "共关闭 $killed 个进程，端口 $Port 已释放。" -ForegroundColor Green
if ($failed -gt 0) { exit 1 }
exit 0
