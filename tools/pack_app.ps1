<#
组装「程序目录」—— release.yml 与 e2e.yml 共用同一份逻辑。

为什么要抽出来：以前两个 workflow 各写了一遍同样的拷贝 + 索引白名单，
2026-10-30 收紧白名单时改了两处，而 e2e 的 fallback 分支还漏了
`web/media/.gitkeep`、`userdata/.gitkeep`、app.json 仓库名替换和白名单硬自检 ——
于是"现场组装"出来的包与真正发布的包不是一个东西，测出来的结论不可信。

用法：
    pwsh tools/pack_app.ps1 -Dst dist/ESCAH-Offline -Repo allindusk/escahoffline
    pwsh tools/pack_app.ps1 -Dst D:\x\ESCAH-Offline          # 不替换仓库名
#>
param(
    [Parameter(Mandatory = $true)][string]$Dst,
    [string]$Repo = ""
)

$ErrorActionPreference = "Stop"

# 程序自带的索引（**白名单**：新增文件默认不进包，要进必须在这里加一行）。
# 以前是黑名单，结果把本机产物一起打包了：imgmap.x2.js（超分记录）、
# imgmap.base.js（WebP 覆盖层）、sfxmap.js/json、stand_slot.json、*.orig。
$INDEX_FILES = @(
    'advindex.js', 'app.json', 'cgmap.js', 'charmeta.js',
    'imgmap.js', 'index.js', 'names.js', 'packs.links.js',
    'stand_layers.json'
)

New-Item -ItemType Directory -Force -Path $Dst | Out-Null
New-Item -ItemType Directory -Force -Path "$Dst/web/data" | Out-Null

Copy-Item server, tools, docs, glossary -Destination $Dst -Recurse
# glossary（译名对照表）只进全量包，**不进纯代码更新包** —— 那是用户会自己
# 维护的数据，更新时不该覆盖（release.yml 里只从 dist 拷 server/tools/docs）。
Copy-Item VERSION, README.md, NOTICE.md, LICENSE, requirements.txt, start.sh `
    -Destination $Dst
# 启动脚本按通配符拷，**不要写死中文文件名**：
# Windows PowerShell 5.1 会把无 BOM 的 UTF-8 脚本按 ANSI 读，中文会变乱码，
# 于是 Copy-Item 找不到 "启动.bat"。通配符没有这个问题。
Get-ChildItem -Filter '*.bat' -File | Copy-Item -Destination $Dst

# serve.py / stop_server.ps1 / dev-*.bat 是「纯前端预览」调试工具，只服务开发，
# 不放进发布包 —— 否则用户会看到两个都叫「启动」的脚本。
Copy-Item web/*.html -Destination "$Dst/web"
Copy-Item web/assets -Destination "$Dst/web" -Recurse

foreach ($f in $INDEX_FILES) {
    $src = Join-Path 'web/data' $f
    if (Test-Path $src) {
        Copy-Item $src -Destination "$Dst/web/data"
    } else {
        Write-Host "::warning::索引缺失：$f"
    }
}

New-Item -ItemType Directory -Force -Path "$Dst/web/media", "$Dst/userdata" | Out-Null
Set-Content -Path "$Dst/web/media/.gitkeep" -Value "" -NoNewline
Set-Content -Path "$Dst/userdata/.gitkeep" -Value "" -NoNewline

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

$n = (Get-ChildItem $Dst -Recurse -File | Measure-Object).Count
Write-Host "web/data 索引检查通过（$((Get-ChildItem "$Dst/web/data" -File).Count) 个文件）"
Write-Host "程序目录组装完成：$n 个文件 → $Dst"
