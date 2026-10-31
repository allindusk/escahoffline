# 给 AI 的接手文档（AI-ONBOARDING）

> 你是被叫来给这个项目做二次开发的 AI。**先把这份文档读完再动手**。
> 它记录了这个项目「为什么这么写」——很多地方看起来可以简化，但那些是踩过坑之后的选择。
> 最后更新：v1.0.2

---

## 0. 一分钟定位

这是一个**离线剧情播放器**，把《超昂大战》（エスカレーションヒロインズ）手游里的
角色剧情做成了可以在浏览器里翻阅的电子书：还原原作演出（背景/立绘/CG/语音/BGM），
带多语言译文，可导出视频与 CG。

**关键前提：仓库里没有任何游戏素材。** 素材（20 GB，24 万个文件）由用户自己
通过「资源包」方式获取。程序 + 数据索引进 Git，素材永久不进 Git。

技术栈刻意保持极简：**Python 标准库 + 原生 JS**，没有构建步骤、没有 npm、没有框架。
原因是这是一个要"下载解压即用"的便携工具，任何构建链都会变成用户的负担。

---

## 1. 目录与职责（只列你要动的）

```
escalation_heroines/escahoffline/
├─ VERSION                     语义化版本号，更新器比对用
├─ 启动.bat / start.sh          用户入口：python -m server --open
├─ requirements.txt            只有 pillow（可选增强）
├─ server/                     ★ 本地服务端
│   ├─ cdn/                    ★★ 资源主链路：官方 CDN → AssetBundle → 本机解包
│   │   ├─ config.py           路径/常量/CDN 域名（可被用户覆盖）
│   │   ├─ manifest.py         清单下载 + AES 解密 + 索引
│   │   ├─ aes.py              AES-256-CBC（有 cryptography 就用，没有退回纯 Python）
│   │   ├─ net.py              并发下载 + size 校验 + 超时按体积放大
│   │   ├─ select.py           bundle 命名规则 → 按角色/剧情选包
│   │   ├─ stages.py           下载→解剧本→语音→图片→立绘合成→刷新映射
│   │   ├─ install.py          ★ 对外门面：install_unit / list_units / unit_state
│   │   ├─ scripts/            解包脚本（UnityFS 解析、立绘合成、映射生成）
│   │   └─ data/               角色名/元数据（随仓库分发；**清单不放这里**，见毒点 11）
│   ├─ paths.py                路径常量、config.json/state.json 读写
│   ├─ jobs.py                 后台任务 + 进度（安装/超分/导出都走它）
│   ├─ net.py                  通用多线程分段下载（Range/断点续传/重试）
│   ├─ packs.py                下载源 links / 旧清单兼容（zip 流程已删，见下方说明）
│   ├─ media.py                超分 / WebP 转码 / 图片覆盖层生成
│   ├─ exports.py              CG 打包、视频落盘、译文导入导出
│   ├─ updater.py              应用自更新（退出后替换白名单文件）
│   ├─ api.py                  HTTP 路由 + 静态站点 + 作业轮询
│   └─ __main__.py             命令行入口
├─ web/                        站点根（浏览器看到的一切）
│   ├─ index.html              播放器
│   ├─ cg.html                 CG 画廊
│   ├─ setup.html              ★ 资源管理控制台（下载/超分/导出/译文/更新/设置）
│   ├─ assets/js/
│   │   ├─ player.js           演出引擎（按 Methods 指令序列复现游戏演出）
│   │   ├─ packstate.js        资源状态层：某部剧情能不能播
│   │   ├─ transui.js          译文导入导出面板
│   │   ├─ tour.js             新手引导引擎（高亮 + 气泡）
│   │   ├─ setup.js            控制台逻辑
│   │   ├─ links.js            下载源解密（与服务端同算法）
│   │   └─ i18n.js             多语言 UI 层
│   ├─ data/                   ★ 索引（进 Git）与剧情正文（不进 Git）
│   │   ├─ names.js charmeta.js index.js advindex.js   角色/章节索引
│   │   ├─ imgmap.js         图片键 → 原图路径
│   │   ├─ imgmap.x2.js      超分覆盖层（可缺）
│   │   ├─ imgmap.base.js    WebP 转码覆盖层（可缺）
│   │   ├─ sfxmap.js / cgmap.js / app.json
│   │   └─ char_<id>.js / adv_<kind>_<no>.js           剧情正文（资源包分发）
│   └─ media/                 素材（永久 gitignore）
├─ tools/                      维护者脚本（构建清单、打包、加密链接）
└─ userdata/                   用户数据（gitignore）：配置/已装/缓存/备份/导出/日志
```

三条最重要的数据链路，改任何东西前先确认你动的是哪条：

1. **播放链路**：`index.html` → `data/index.js` + `data/char_<id>.js` → `player.js`
   按 `parts[].methods[]` 指令序列演出（`ShowBackground` / `ShowStand` / `ShowCG` /
   `ShowMessage` / `VOICE` / `WaitMotion` …），时序模型见 `player.js` 顶部注释。
2. **资源链路（最重要，别搞错）**：UI 勾一部剧情 → `POST /api/download`
   → `server/cdn/install.py::install_unit()` → 清单算依赖 → `cdn/net.py` 并发下载
   AssetBundle → `cdn/stages.py` 解剧本/语音/图片 + 合成立绘 → 写 `web/data` 与 `web/media`
   → `/api/state` 用 `unit_state()` 判定"能不能播"。
   **没有任何预打包的 zip**；素材每次按需从官方 CDN 拉并在本机解包。
   旧的 `packs.json` / depot 概念**已彻底删除**（连同 `run_pipeline` 那套 zip 下载流程）。
现在只有官方 CDN 一条路：清单 → 按剧本引用反查 bundle → 下载 → 解包。
3. **图片解析链路**（改图片相关功能必读）：
   `candsOf(kind, key)` 返回候选数组，顺序是
   `__IMGX2`（本地超分）→ `__IMGBASE`（本地转码）→ `__IMGMAP`（出厂原图），
   `<img>` 用 `applyChain()` 逐级 `onerror` 回退。**任何新加的取图代码都必须走这条链**，
   直接写 `media/img_x2/...` 会在"用户只下了原图"时裂图（这个 bug 修过一次）。

---

## 2. 硬性约定（违反了会坏）

| 约定 | 原因 |
|---|---|
| 不把 `web/media/**`、`web/data/char_*.js`、`web/data/adv_*.js` 提交进 Git | 20 GB + 版权；`.gitignore` 已排除，别用 `git add -f` |
| 不修改 `data/imgmap.js` / `imgmap.x2.js` 的语义 | 它们由 `tools/build_imgmaps.py` 生成，覆盖层才是"用户态"改动点 |
| 新增图片取图处一律用 `candsOf` + `applyChain` | 三层回退是资源可选安装的前提 |
| 新增耗时操作一律走 `jobs.submit()` + 前端轮询 `/api/jobs` | 不要用 SSE 长连接：会让 headless 截图/某些代理环境永远"加载中" |
| `/api/state` 保持轻量（<100 ms） | 播放器和控制台打开就会请求；磁盘遍历类统计放 `/api/space`（整个目录按用途分桶，一次遍历）与 `/api/stats`，且必须带缓存 |
| 更新只替换白名单文件（`server/` `web/assets/` 等） | `web/media`、`web/data`、`userdata` 是用户数据，覆盖即丢失 |
| 前端改动必须无头截图肉眼确认 | 语法检查通过≠页面正常，见 §5 |
| UI 文案用中文、注释解释"为什么" | 目标用户是中文小白 |
| CDN 域名一律走 `config.cdn_base()`，不写死 | 加密存放 + 用户可覆盖（官方换域名不用改代码） |
| `requirements.txt` 必须包含 UnityPy / lz4 / Pillow / numpy | 少任一个，用户一点"安装"就失败，且报错很难懂 |
| 改 `cdn/scripts/*.py` 时注意 cwd 约定 | 它们用 `../web` 与 `files_img` 相对路径，cwd 必须是 `userdata/` |

---

## 3. 常见任务 Recipe

### A. 改界面文案 / 加一个新的设置项
1. `web/index.html` 加控件（跟随现有 `.row` 结构）
2. `web/assets/js/player.js`：`DEFAULTS` 加默认值 → `syncSettingControls()` 初始化 →
   绑定 `onchange` → 存 `localStorage`（`applySettings()` 已经统一处理）
3. 若新增可见样式，写进 `web/assets/css/extras.css`，别改 `player.css`

### B. 加一个后端接口
1. `server/api.py`：GET 放 `_api_get()`，POST 放 `_api_post()`，返回 `self._json(...)`，
   错误用 `self._err("人话原因", 400)`
2. 耗时 >0.3 s 的，改成 `jobs.submit(...)` 返回 `{"job": ...}`
3. 前端 `setup.js` 里用 `api('xxx', {method:'POST', body: JSON.stringify(...)})`

### C. 改资源包划分
1. 只改 `tools/build_packs.py`（引用分析是唯一真相）
2. 跑 `python tools/build_packs.py`（自带三项完备性校验，必须全 `[OK]`）
3. 重新打包：`python tools/make_pack_archives.py --only core,shared_back`
4. 上传 → 用户点「检查资源列表更新」

### D. 加语言 / 改翻译机制
- UI 文案：`web/assets/i18n/*.json`（`i18n.js` 加载），`data-i18n` 属性驱动
- 剧情译文：`web/assets/i18n/zh-Hans/<unit>.json`，**key 是日文原文**，`messages` 子字典
- 导入导出走 `server/exports.py` 的 `export_trans` / `import_trans`，
  块式格式（一句一个 `### 编号 【说话人】 (N行)` 块，块内 `ja:` / `zh:`），
  文件头自带给 AI 的说明——改格式要同步改文件头

### E. 改演出表现
- 所有指令在 `player.js` 的 `playPart` 主循环 `case 'XxxMethod'` 里
- 时序模型：画面/文字/语音**立即启动不阻塞**（累积到 `ST.pendingAll`），
  `WaitMotion` 才是同步点。改错会导致"字幕先出图后到"这类时序问题
- 改完用 `?seek=N`（跳到第 N 条指令）+ 截图验证

### F. 移植到别的游戏/别的剧情格式
- 换掉 `data/*.js` 的产出方即可，只要保持 `parts[].methods[]` 结构
- 参考 `docs/数据格式.md`（字段含义与全部 Method 语义）

---

## 4. 验证清单（提交前必做）

```bash
# 1. 后端语法与导入
python -c "import server.api, server.media, server.exports, server.updater"

# 2. 前端语法（node 24 可用）
for f in web/assets/js/*.js; do node --check "$f" || echo "FAIL $f"; done

# 3. 资源清单完备性（唯一归属 / 文件存在 / 单元覆盖）
python tools/build_packs.py --check-only

# 4. 起服务 + 冒烟
python -m server -p 8123
curl -s http://127.0.0.1:8123/api/state | head -c 200
curl -s -o /dev/null -w "%{time_total}s\n" http://127.0.0.1:8123/api/stats   # 应 <0.5s
```

### 无头截图（关键，别省）

```powershell
& "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" `
  --headless=new --disable-gpu --no-sandbox --hide-scrollbars `
  --timeout=20000 --user-data-dir=%TEMP%\p1 --virtual-time-budget=6000 `
  --screenshot=%TEMP%\x.png --window-size=1500,950 `
  "http://127.0.0.1:8123/setup.html?notour=1#packs"
```

然后**用图片查看器真的看一眼**。已知坑：

- 每次用**不同的** `--user-data-dir`，或在截图前清掉残留的 `msedge.exe`
  （`Get-CimInstance Win32_Process -Filter "Name='msedge.exe'"` 里 CommandLine 含你的
  profile 路径的那些）。残留进程会占住 profile，导致后续 `--screenshot` 立刻返回、
  根本不截图。
- 页面里如果有长连接（SSE/WebSocket）或秒级定时器在跑重活，`virtual-time-budget`
  会一直等到预算耗尽。测试钩子：`?notour=1` 跳过引导。
- 播放器有内置调试面板：按 `D` 显示 `idx/run/bg/stand/cg/window` 与最近 JS 报错，
  排查"某处 null 抛错导致流程中断"最快。

---

## 5. 已知毒点（改到相关代码时留意）

1. **`imgmap` 混装过超分**：早期 `imgmap.js` 的值里混着 `img_x2`，导致只下原图的
   用户满屏裂图。现在已拆成 `imgmap.js`(原图) + `imgmap.x2.js`(超分)，
   生成脚本 `tools/build_imgmaps.py` 可重复跑。别把两者合并回去。
2. **`/api/stats` 曾要 2.1 秒**：遍历 24 万文件。现在 `os.scandir` + 30 秒缓存 +
   前端懒加载。新增统计类接口照这个模式做。
3. **SSE 曾让无头截图卡死**：`/api/jobs/stream` 保留但前端已改用轮询。别在前端再引 SSE。
4. **动作方法名大小写不一**：数据里既有 `Talk` 也有 `ShowMessage` 这类同义指令，
   `player.js` 里兼容处理了，新增分支别只认一种。
5. **`HideStand` 的 `Position:"All"`** 是"清空全场"，不是站位名（曾导致立绘只增不减）。
6. **Windows 控制台编码**：`tools/*.py` 都做了 `sys.stdout.reconfigure(utf-8)`；
   自己写新脚本时记得加，否则中文输出在 PowerShell 里是乱码/报错。
7. **便携包里的 Python 是 embedded 版**：没有 `pip`，`site-packages` 靠
   `python3xx._pth` 打开 `import site` 才能用。CI 里已处理，本地改依赖要同步
   `.github/workflows/release.yml`。
8. **`manifest.has_manifest()` 曾经只查项目根**，导致随仓库分发的种子清单失效、
   首次启动又去下载 5 MB 并用纯 Python 解密 —— 接口卡 40 秒。现在两处都查。
9. **纯 Python AES 解 5 MB 要 30–60 秒**：`cdn/aes.py` 已加 `cryptography` /
   `pycryptodome` 快路径（毫秒级），两者都没有才退回慢实现。`requirements.txt`
   里 `cryptography` 是"建议装"，不装也能跑就是慢。
10. **`select.Manifest` 的返回值不统一**：`scenario()/voices()/meta_images()` 返回
    **清单条目 dict**，`back_bundle()/stand_bundle()/cg_bundle()` 返回**包名字符串**。
    `cdn/install.py::_entries()` 统一处理了，新代码请复用它，别自己假设。
11. **资源清单不进仓库**（`.gitignore` 里 `Manifest*Ex.*`）：它会随官方更新变化，
    第一版曾把它当种子提交，结果仓库里躺着 4.6 MB 必然过期的数据。
    代价是"首次使用要先下载 5 MB"，因此 `unit_status_map()` 在无清单时
    **不能**调 `list_units()`（会触发下载）——它挂在 `/api/state` 上，一调就让
    控制台首屏卡十几秒，"请先下载清单"的引导还永远出不来。
12. **下载线路：pip 测速、GitHub 固定顺序**（2026-11-01 定版，别改回去）：
    `server/mirrors.py` 里两组路线两套策略 ——
    * pip 镜像：`best("pip")` / `ranked("pip")` **测速选最快**并缓存 30 分钟。
      不要在代码里写死镜像地址：实测同一时刻 PyPI 官方 288 KB/s、华为云 11.9 MB/s，
      差 40 倍，而且哪条最快因网络而异。
    * GitHub（release / API / raw）：`ranked("github")` **不测速**，直接按
      `GH_ROUTES` 顺序返回（gh-proxy 主站 → 优选 v4 → 优选 v4/v6 → Fastly →
      官方直连兜底）。判定标准只有"有没有正常响应"，有响应就用、不管快慢；
      四条反代全无响应才落到官方直连。**不要再加测速/打分/按速度重排**。

---

## 6. 如果你是来做「翻译」的

这是本项目投入产出最高的方向，具体流程见 `docs/翻译指南.md`。要点：

- 译文文件在 `web/assets/i18n/zh-Hans/<unit>.json`，`{"messages": {"日文原文": "中文"}}`
- 导出格式是块式：一句一个 `### 编号 【说话人】 (N行)` 块，块内 `ja:` 原文 / `zh:` 译文；
  文件头有给人/AI 的说明，**不要改块头编号和 ja: 原文**（译文行数不能超过 N）
- 发言人（对话框上方的名字）单独一张全局表 `zh-Hans/_speakers.json`，全站共用
- 术语统一用 `output/custom_dict.json`（日文 Galgame 术语表）
- 日文「ダメージを回復」是"恢复**所受伤害**"，「スタミナを回復」才是"恢复体力"，别混
- 露骨内容按语义逐句判断：只有真正露骨才跳过，拟声/惊叫/擦边照常翻译
