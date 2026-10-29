/* 免责声明（网页版）
 *
 * 为什么网页上也要有，而不只是 README：
 * 用户真正接触的是这三个页面，很多人根本不会去看仓库。README 里的声明只有
 * 打开 GitHub 才看得到，而"别让我因为用这个工具变成违法人员"这件事必须在
 * 用户动手之前讲清楚。
 *
 * 行为：
 *   · 首次访问（每个浏览器一次）强制展示，必须点过"我已了解"才能用
 *   · 之后可通过每个页面右下角的「免责声明」再次查看
 *   · 测试钩子：URL 加 ?nodisclaimer=1 跳过（无头截图/自动化用）
 *
 * 文案与仓库根目录 NOTICE.md 保持一致；NOTICE 是完整版，这里是必须让用户
 * 看到的精简版。
 */
window.DISCLAIMER = (() => {
  const KEY = 'escah_disclaimer_v1';

  const CSS = `
#dlMask{position:fixed;inset:0;z-index:10000;background:rgba(4,7,12,.93);
  display:flex;align-items:center;justify-content:center;padding:20px}
#dlMask .box{width:min(680px,96vw);max-height:88vh;overflow:auto;background:#0d1622;
  border:1px solid #2b4a6b;border-radius:16px;padding:26px 30px;color:#dbe8f6;
  box-shadow:0 24px 80px #000;font-size:13.5px;line-height:1.85}
#dlMask h3{margin:0 0 4px;font-size:19px;color:#ffd77a}
#dlMask .sub{color:#7d92ab;font-size:12px;margin-bottom:16px}
#dlMask ol{margin:0 0 16px;padding-left:22px}
#dlMask li{margin-bottom:9px;color:#c7d9ec}
#dlMask b{color:#fff}
#dlMask .key{border-left:3px solid #c9505a;background:#241417;padding:10px 14px;
  border-radius:8px;margin:0 0 16px;color:#ffc9cf}
#dlMask .ft{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
#dlMask button{border-radius:9px;padding:9px 18px;font-size:13px;cursor:pointer;
  border:1px solid #2f5478;background:#16283c;color:#dff0ff}
#dlMask button.primary{background:#2b6ea8;border-color:#4b96d4;font-weight:600}
#dlMask button:hover{filter:brightness(1.15)}
#dlMask a{color:#7adcff;text-decoration:none}
/* 右下角常驻条：版本号与免责声明是**两个独立标签**，只是并排摆放，
   避免用户误以为 "v1.0.0" 是声明的版本号 */
#dlBar{position:fixed;right:12px;bottom:10px;z-index:10100;
  display:flex;align-items:center;gap:8px}
#appVer{font-size:11px;color:#4d5f74;background:rgba(10,16,24,.7);
  border:1px solid #1a2836;border-radius:7px;padding:4px 9px;
  letter-spacing:.3px;user-select:text}
#dlFoot{font-size:11.5px;display:flex;align-items:center;gap:6px;
  color:#6b8199;background:rgba(10,16,24,.9);border:1px solid #1e3044;
  border-radius:8px;padding:5px 11px;cursor:pointer;
  box-shadow:0 4px 14px rgba(0,0,0,.45);transition:color .15s,border-color .15s}
#dlFoot:hover{color:#9fc7e8;border-color:#2f5478}
`;

  function style() {
    if (document.getElementById('dlCss')) return;
    const s = document.createElement('style');
    s.id = 'dlCss';
    s.textContent = CSS;
    document.head.appendChild(s);
  }

  function footer() {
    if (document.getElementById('dlBar')) return;
    // **必须先注入样式**：否则首次创建时元素还没拿到 position:fixed，
    // 会按普通块级元素排在 body 末尾（左下角、白字），
    // 直到点开弹窗调用 style() 才"跳"到右下角。这就是用户看到的割裂。
    style();
    const bar = document.createElement('div');
    bar.id = 'dlBar';
    bar.innerHTML =
      '<span id="appVer" title="程序版本号">v—</span>' +
      '<div id="dlFoot" title="使用条款与版权声明（点开可再看一遍）">免责声明</div>';
    document.body.appendChild(bar);

    bar.querySelector('#dlFoot').onclick = () => show();
    // 版本号从本地服务取（项目版本，不是声明版本）；纯静态打开时显示 v—
    try {
      fetch('api/state', { cache: 'no-store' })
        .then(r => (r.ok ? r.json() : null))
        .then(d => {
          const e = document.getElementById('appVer');
          if (e && d && d.version) e.textContent = 'v' + d.version;
        })
        .catch(() => { });
    } catch (e) { /* 静态打开，忽略 */ }
  }

  function show(force) {
    style();
    if (document.getElementById('dlMask')) return;
    const m = document.createElement('div');
    m.id = 'dlMask';
    m.innerHTML = `
<div class="box">
  <h3>使用前请确认</h3>
  <div class="sub">本项目仅供个人学习与研究使用 · 请读完这 5 条再继续</div>

  <div class="key">
    <b>本项目不包含任何游戏素材</b>，也不提供素材下载地址。<br>
    仓库与安装包里没有一张图、一段语音、一句剧本原文。
  </div>

  <ol>
    <li><b>与官方无关</b>：本项目是第三方工具，与游戏开发/发行方没有任何隶属、
      合作或授权关系，也未获其认可。</li>
    <li><b>素材需自行合法获取</b>，其著作权归各自权利人所有。你使用本工具
      浏览的任何素材都不属于本项目，本项目也不对其主张任何权利。</li>
    <li><b>请勿传播素材、请勿商用、请勿高调宣传。</b>不要打包对外分发，
      不要发到公开社交平台/视频站/大群做推广，不要用于任何盈利场景
      （含付费社群、网盘拉新、接广告）。私下自用可以，扩大传播会直接提高
      你和本项目面临法律风险的概率。</li>
    <li><b>风险自负</b>：本工具按"现状"提供，不附带任何担保；因使用本工具
      （含数据丢失、设备损坏、账号风险、法律纠纷）产生的一切后果由使用者承担。</li>
    <li><b>权利人可要求下架</b>：如果你是权利人并认为本项目不当，请在仓库
      Issues 说明，作者会第一时间处理（删改或停止维护）。</li>
  </ol>

  <div class="ft">
    <button class="primary" id="dlOk">我已了解，继续使用</button>
    <button id="dlFull">查看完整声明</button>
    <span class="sub" style="margin:0">完整版见 <code>notice.html</code>（本机打开）</span>
  </div>
</div>`;
    document.body.appendChild(m);
    m.querySelector('#dlOk').onclick = () => {
      try { localStorage.setItem(KEY, '1'); } catch (e) { }
      m.remove();
      document.dispatchEvent(new CustomEvent('disclaimer:accepted'));
    };
    // 打开本地完整声明页，不跳 GitHub —— 用户可能压根没网/没仓库地址
    m.querySelector('#dlFull').onclick = () => {
      window.open('notice.html', '_blank', 'noopener');
    };
    if (!force) {
      // 非强制时点遮罩也能关（方便用户回看后继续）
      m.querySelector('#dlOk').textContent = '关闭';
    }
  }

  function auto() {
    try {
      if (new URLSearchParams(location.search).has('nodisclaimer')) { footer(); return; }
    } catch (e) { }
    let seen = '';
    try { seen = localStorage.getItem(KEY) || ''; } catch (e) { }
    if (!seen) show(true);
    footer();
  }

  return { show, auto };
})();
