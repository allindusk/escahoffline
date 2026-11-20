<#
组装「程序目录」—— release.yml 与 e2e.yml 共用同一份逻辑。

为什么要抽出来：以前两个 workflow 各写了一遍同样的拷贝 + 索引白名单，
2026-10-30 收紧白名单时改了两处，而 e2e 的 fallback 分支还漏了
`web/media/.gitkeep`、`userdata/.gitkeep`、app.json 仓库名替换和白名单硬自检 ——
于是"现场组装"出来的包与真正发布的包不是一个东西，测出来的结论不可信。

**清单从哪来**：`server/update_manifest.json`，与更新器（server/updater.py）
共用同一份。以前这里是手写的 `$INDEX_FILES`，那边是手写的
`WEB_DATA_UPDATABLE`，新增文件要改两处、漏一处就出事故（shell.html 404、
cgmap 分组不对、stand_box 气泡错位都是这么来的）。现在只改清单一处。

读不到清单**直接失败**（打包不能靠猜）；更新器那边则退回内置常量，
保证"清单坏掉"不会卡死已发布用户的升级。

用法：
    pwsh tools/pack_app.ps1 -Dst dist/ESCAH-Offline -Repo allindusk/escahoffline
    pwsh tools/pack_app.ps1 -Dst D:\x\ESCAH-Offline          # 不替换仓库名
#>
param(
    [Parameter(Mandatory = $true)][string]$Dst,
    [string]$Repo = ""
)

$ErrorActionPreference = "Stop"

$Root = Split-Path $PSScriptRoot -Parent
$ManifestPath = Join-Path $Root "server/update_manifest.json"
if (-not (Test-Path $ManifestPath)) {
    throw "缺少清单：server/update_manifest.json（打包必须有它，不能靠猜）"
}
# **必须显式按 UTF-8 读**：PS 5.1 的 Get-Content 会把无 BOM 的 UTF-8 当 ANSI，
# 清单里的中文路径（启动.bat / 停止.bat）会变乱码，后面 Copy-Item 全部落空。
$Manifest = [System.IO.File]::ReadAllText($ManifestPath, [System.Text.Encoding]::UTF8) |
    ConvertFrom-Json
if ($Manifest._schema -ne 1) {
    throw "清单 schema 不是 1（$($Manifest._schema)），打包脚本不认识"
}

# 程序目录里的索引表（web/data 下），来自清单 data_files 的 pack=true 项
$INDEX_FILES = @($Manifest.data_files.PSObject.Properties |
    Where-Object { $_.Value.pack } |
    ForEach-Object { Split-Path $_.Name -Leaf })

New-Item -ItemType Directory -Force -Path $Dst | Out-Null
New-Item -ItemType Directory -Force -Path "$Dst/web/data" | Out-Null

# ---- 整目录：清单 program_dirs + pack_only_dirs ----------------------------
# 注意嵌套目录（web/assets/）要拷到它的父目录下，否则会变成 $Dst/assets。
foreach ($d in @($Manifest.program_dirs) + @($Manifest.pack_only_dirs)) {
    if (-not $d) { continue }
    $src = $d.TrimEnd('/').TrimEnd('\')
    if (-not (Test-Path $src)) { throw "清单里的目录不存在：$src" }
    $parent = Split-Path $src -Parent
    $dest = if ($parent) { Join-Path $Dst $parent } else { $Dst }
    # 父目录先建：否则嵌套目录（web/assets/）会被当成目标本体，内容铺到上一层。
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    Copy-Item $src -Destination $dest -Recurse
}

# ---- 单文件：清单 program_files -------------------------------------------
foreach ($f in @($Manifest.program_files)) {
    if (-not $f) { continue }
    if ($f -match '[*?]') {
        # 通配（web/*.html）：目标 = 它的父目录
        $parent = Split-Path $f -Parent
        Copy-Item $f -Destination (Join-Path $Dst $parent) -Force
    } else {
        if (-not (Test-Path $f)) { throw "清单里的文件不存在：$f" }
        Copy-Item $f -Destination $Dst -Force
    }
}

# 启动脚本按通配符拷，**不要写死中文文件名**：
# Windows PowerShell 5.1 会把无 BOM 的 UTF-8 脚本按 ANSI 读，中文会变乱码，
# 于是 Copy-Item 找不到 "启动.bat"。通配符没有这个问题。
Get-ChildItem -Filter '*.bat' -File | Copy-Item -Destination $Dst

# ---- web/data 索引：清单 data_files 的 pack=true ---------------------------
foreach ($p in $Manifest.data_files.PSObject.Properties) {
    if (-not $p.Value.pack) { continue }
    $rel = $p.Name.Replace('\', '/')
    $destDir = Join-Path $Dst (Split-Path $rel -Parent)
    New-Item -ItemType Directory -Force -Path $destDir | Out-Null
    if (Test-Path $rel) {
        Copy-Item $rel -Destination $destDir -Force
    } else {
        Write-Host "::warning::清单里的索引缺失：$rel"
    }
}

# ---- web/assets/i18n：只留清单里 pack=true 的程序文件 ----------------------
# web/assets 是整目录拷的，会把用户自己攒的译文（<lang>/*.json）一起带进发布包。
# 那是用户数据（.gitignore 里），绝不能出现在分发包里 —— 体积大、还等于把
# 别人的翻译发给新用户。清单的 data_files 里只有 ui.json 是 pack=true。
$i18n = Join-Path $Dst "web/assets/i18n"
$keep = @($Manifest.data_files.PSObject.Properties |
    Where-Object { $_.Value.pack -and $_.Name -like 'web/assets/i18n/*' } |
    ForEach-Object { Split-Path $_.Name -Leaf })
if (Test-Path $i18n) {
    Get-ChildItem $i18n -Force |
        Where-Object { $_.Name -notin $keep } |
        Remove-Item -Recurse -Force
    $drop = Get-ChildItem $i18n -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -notin $keep }
    if ($drop) { throw "web/assets/i18n 混进了非程序文件：$($drop.Name -join ', ')" }
}

# 清掉 Python 缓存：release.yml 的「后端导入自检」会先 import server.*，
# runner 上于是有了 server/**/__pycache__/*.pyc，而上面是 -Recurse 整目录拷贝
# → v1.0.3 的 portable/update 包里混进了 25 个 .pyc（跨机器无用，纯脏东西）。
Get-ChildItem $Dst -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem $Dst -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Extension -in '.pyc', '.pyo' } |
    Remove-Item -Force -ErrorAction SilentlyContinue

# 把 app.json 里的占位仓库名换成真实仓库，用户开箱即可检查更新。
# **必须显式按 UTF-8 读写**：Get-Content/Set-Content 的默认编码随 PowerShell 版本
# 变化（5.1 读无 BOM 文件当 ANSI），实测会把 app.json 里的中文说明改成乱码。
if ($Repo) {
    $app = (Resolve-Path "$Dst/web/data/app.json").Path
    $raw = [System.IO.File]::ReadAllText($app, [System.Text.Encoding]::UTF8)
    $new = $raw -replace 'YOUR_GITHUB_NAME/[^"]+', $Repo
    [System.IO.File]::WriteAllText($app, $new, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "app.json 仓库名 → $Repo"
}

# 硬自检：web/data 里只能有上面那几个索引。多出任何文件都说明本机产物混进了包
# （曾因此让新用户打开画质页看到"没有需要超分的图（都已做过）"）。
$extra = Get-ChildItem "$Dst/web/data" -File | Where-Object { $_.Name -notin $INDEX_FILES }
if ($extra) {
    $names = ($extra | Select-Object -ExpandProperty Name) -join ', '
    throw "程序目录里混进了本机产物：$names"
}

# 同样的硬自检：Python 缓存一律不许进包（.pyc 是构建机上那条解释器编的，
# 跨机器没用；还会让用户看到一堆陌生的 .pyc 而怀疑包不对）
$junk = Get-ChildItem $Dst -Recurse -File |
    Where-Object { $_.Extension -in '.pyc', '.pyo' }
if ($junk) {
    throw "程序目录里混进了 Python 缓存：$($junk[0].FullName)（共 $($junk.Count) 个）"
}

$n = (Get-ChildItem $Dst -Recurse -File | Measure-Object).Count
Write-Host "web/data 索引检查通过（$((Get-ChildItem "$Dst/web/data" -File).Count) 个文件）"
Write-Host "程序目录组装完成：$n 个文件 → $Dst"
