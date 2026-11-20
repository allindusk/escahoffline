<#
从「程序目录」（tools/pack_app.ps1 的产物）组装纯代码更新包。

为什么也读清单：以前 release.yml 里手写了一遍要拷的目录和文件名
（server/tools/docs + VERSION/README/NOTICE/LICENSE/start.sh/启动.bat + web/*.html），
于是新增一个根文件要记得改两处，漏了就"更新包带了但没拷进去"。
现在与 pack_app.ps1、server/updater.py 共用 server/update_manifest.json 一份清单。

不含：素材（web/media）、用户数据（userdata、i18n 用户译文）、超分引擎
（tools/realcugan，约 80 MB）、glossary（用户会自己维护）。

用法：
    pwsh tools/pack_update.ps1 -Src dist/ESCAH-Offline -Dst dist/ESCAH-Offline-update
#>
param(
    [Parameter(Mandatory = $true)][string]$Src,
    [Parameter(Mandatory = $true)][string]$Dst
)

$ErrorActionPreference = "Stop"

$ManifestPath = Join-Path $Src "server/update_manifest.json"
if (-not (Test-Path $ManifestPath)) {
    throw "程序目录里没有清单：$ManifestPath"
}
$Manifest = [System.IO.File]::ReadAllText($ManifestPath, [System.Text.Encoding]::UTF8) |
    ConvertFrom-Json
if ($Manifest._schema -ne 1) {
    throw "清单 schema 不是 1（$($Manifest._schema)）"
}

if (Test-Path $Dst) { Remove-Item $Dst -Recurse -Force }
# **必须先建出 $Dst 本身**：Copy-Item 在"目标是尚不存在的目录"时会把源目录
# 当成目标本体（server 的内容直接铺到 $Dst 根，目录名丢失），而不是建 $Dst/server。
New-Item -ItemType Directory -Force -Path $Dst | Out-Null

# ---- 整目录：program_dirs（含 web/assets/，打包时已剔除用户译文）-----------
foreach ($d in @($Manifest.program_dirs)) {
    if (-not $d) { continue }
    $rel = $d.TrimEnd('/').TrimEnd('\')
    $from = Join-Path $Src $rel
    if (-not (Test-Path $from)) { throw "程序目录里缺少：$rel" }
    $parent = Split-Path $rel -Parent
    $dest = if ($parent) { Join-Path $Dst $parent } else { $Dst }
    # **父目录必须先存在**：Copy-Item 在"目标目录尚不存在"时会把源目录当成目标本体，
    # 于是 web/assets 的内容被铺到 $Dst/web 下（生成 web/js 而不是 web/assets/js）。
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    Copy-Item $from -Destination $dest -Recurse
}

# 超分引擎连同模型约 80 MB，不进「纯代码更新包」；它在 dist 里由 release.yml
# 在**打包更新包之后**才放进去，这里删一次是兜底（手动装配顺序错了也不至于发大包）。
Remove-Item (Join-Path $Dst "tools/realcugan") -Recurse -Force -ErrorAction SilentlyContinue

# ---- 单文件：program_files（根文件 + web/*.html）--------------------------
foreach ($f in @($Manifest.program_files)) {
    if (-not $f) { continue }
    if ($f -match '[*?]') {
        $parent = Split-Path $f -Parent
        Get-ChildItem (Join-Path $Src $f) -File -ErrorAction SilentlyContinue |
            Copy-Item -Destination (Join-Path $Dst $parent)
    } else {
        $from = Join-Path $Src $f
        if (-not (Test-Path $from)) { Write-Host "::warning::程序目录里缺少：$f" ; continue }
        Copy-Item $from -Destination $Dst -Force
    }
}

# ---- 程序索引：只要清单里 pack=true 的项（web/data 与 i18n/ui.json）-------
foreach ($p in $Manifest.data_files.PSObject.Properties) {
    if (-not $p.Value.pack) { continue }
    $rel = $p.Name.Replace('\', '/')
    $from = Join-Path $Src $rel
    if (-not (Test-Path $from)) { Continue }   # web/data 里可能没这份（本机构建略过）
    $destDir = Join-Path $Dst (Split-Path $rel -Parent)
    New-Item -ItemType Directory -Force -Path $destDir | Out-Null
    Copy-Item $from -Destination $destDir -Force
}

# ---- i18n 只留 ui.json -----------------------------------------------------
$i18n = Join-Path $Dst "web/assets/i18n"
if (Test-Path $i18n) {
    $keep = @($Manifest.data_files.PSObject.Properties |
        Where-Object { $_.Value.pack -and $_.Name -like 'web/assets/i18n/*' } |
        ForEach-Object { Split-Path $_.Name -Leaf })
    Get-ChildItem $i18n -Force |
        Where-Object { $_.Name -notin $keep } |
        Remove-Item -Recurse -Force
}

# ---- 兜底种子 --------------------------------------------------------------
# **执行覆盖的是升级前那版程序生成的更新器脚本**，它只认当时那份清单 —— 升级前
# 那份清单里没有的文件（v1.1.6 的 shell/stand/music.html、v1.1.3 的 stand_box.js）
# 会被挡掉，升级完自动打开的 /shell.html 直接 404。这里把页面与关键程序索引按
# **相同相对路径**再放一份进 web/assets/seed/，由新版本启动时补到缺失的位置
# （server/__main__.py::_heal_seed_files）。seed 路径里不能出现 i18n 段，
# 否则老版本的黑名单会在解压阶段就把它滤掉 → 界面文案放 extras/i18n-ui.json。
$seed = Join-Path $Dst "web/assets/seed/web"
New-Item -ItemType Directory -Force -Path (Join-Path $seed "data") |
    Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Dst "web/assets/seed/extras") |
    Out-Null
Get-ChildItem (Join-Path $Src "web") -Filter *.html -File |
    Copy-Item -Destination $seed
Copy-Item (Join-Path $Src "web/data/stand_box.js") -Destination (Join-Path $seed "data") `
    -ErrorAction SilentlyContinue
Copy-Item (Join-Path $Src "web/assets/i18n/ui.json") `
    -Destination (Join-Path $Dst "web/assets/seed/extras/i18n-ui.json") `
    -ErrorAction SilentlyContinue
if (-not (Test-Path (Join-Path $seed "shell.html"))) {
    throw "种子目录没生成（缺 shell.html）"
}
if (-not (Test-Path (Join-Path $Dst "web/assets/seed/extras/i18n-ui.json"))) {
    throw "种子目录没生成（缺 i18n-ui.json）"
}

$n = (Get-ChildItem $Dst -Recurse -File | Measure-Object).Count
Write-Host "更新包组装完成：$n 个文件 → $Dst"
