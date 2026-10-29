// 三页共用的顶栏。以前每个页面各写一份 <header>：
//   · 标题不统一（播放器/CG 是「超昂大战」，资源管理是「资源管理」）
//   · 导航字号/内边距/圆角各不同，切页时看着像换了个站点
//   · 当前页高亮是写死在 HTML 里的
// 现在只在这里生成一次，各页只保留自己的右上角控件。
(function () {
  'use strict';

  const NAV = [
    { href: 'index.html', text: '角色剧情' },
    { href: 'cg.html', text: '角色 CG' },
    { href: 'setup.html', text: '资源管理' },
  ];

  // 每页右上角有什么，也在这里声明（id 保持原样，各自 JS 照旧能找到）
  const EXTRA = {
    'index.html': ['lang', 'help'],
    'cg.html': [],
    'setup.html': ['stat', 'help'],
  };

  const CTRL = {
    lang: '<button id="btnLang" title="切换显示语言（L）">日本語</button>',
    help: '<button id="btnHelp" title="新手教程：一步步告诉你每个按钮干什么（H）">?</button>',
    stat: '<span id="hdrStat"></span>',
    help2: '<button id="btnHelp2" title="新手教程（H）">?</button>',
  };

  //: 项目仓库。放在顶栏最右，三页都能点。
  //: 图标用内联 SVG（不依赖外部图片，离线也能显示）。
  const REPO = 'https://github.com/allindusk/escahoffline';
  const GH_LINK =
    '<a id="ghLink" href="' + REPO + '" target="_blank" rel="noopener noreferrer" '
    + 'title="项目开源地址：' + REPO.replace('https://', '') + '">'
    + '<svg viewBox="0 0 16 16" width="17" height="17" fill="currentColor" '
    + 'aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 '
    + '5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94'
    + '-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 '
    + '1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87'
    + '.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 '
    + '2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56'
    + '.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 '
    + '1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8z"/>'
    + '</svg></a>';

  // 配色**写死**，不用站点 CSS 变量：
  //   --gold / --bg2 这些在三个页面里定义不一致（资源管理页根本没有 --gold），
  //   用变量会导致顶栏在不同页面换色，甚至文字变成背景色看不见。
  // 高度也固定 50px —— 三页各自旧规则里的 43 / 37 / 50px 就是这么来的。
  const CSS = `
header[data-shared]{box-sizing:border-box;display:flex;align-items:center;
  gap:14px;padding:0 16px;height:50px;min-height:50px;max-height:50px;
  position:relative;flex-shrink:0;background:#141822;
  border-bottom:1px solid #2a3040;
  font-family:"Microsoft YaHei","Hiragino Sans","Noto Sans JP",system-ui,sans-serif}
header[data-shared] h1{font-size:15px;font-weight:600;color:#e0b566;
  letter-spacing:.5px;margin:0;white-space:nowrap;line-height:1.2}
header[data-shared] h1 small{font-size:11px;color:#6b7488;font-weight:400;
  margin-left:8px;letter-spacing:0}
header[data-shared] nav{position:absolute;left:50%;transform:translateX(-50%);
  display:flex;gap:4px}
header[data-shared] nav a{padding:5px 12px;border-radius:8px;font-size:12.5px;
  text-decoration:none;color:#9aa4b8;background:#1c2230;border:1px solid transparent;
  line-height:1.4;transition:color .15s,background .15s}
header[data-shared] nav a:hover{color:#e6e9f0;background:#243040}
header[data-shared] nav a.on{color:#0d1016;background:#e0b566;font-weight:600}
/* 右侧第一个控件负责把后续内容推到右边（由 JS 打标） */
header[data-shared] .hdr-first{margin-left:auto}
header[data-shared] #hdrStat{font-size:11.5px;color:#6b7488}
/* GitHub 图标：和「？」按钮同尺寸的方形按钮 */
header[data-shared] #ghLink{flex:none;width:28px;height:26px;display:flex;
  align-items:center;justify-content:center;border-radius:8px;
  color:#9aa4b8;background:#1c2230;border:1px solid #2a3040;
  text-decoration:none;transition:color .15s,background .15s,border-color .15s}
header[data-shared] #ghLink:hover{color:#e6e9f0;background:#243040;
  border-color:#3a4a60}
header[data-shared] button{flex:none;padding:3px 10px;border-radius:8px;
  font-size:12px;line-height:1.4;cursor:pointer;color:#9aa4b8;background:#1c2230;
  border:1px solid #2a3040;font-family:inherit}
header[data-shared] button:hover{color:#e6e9f0;border-color:#3a4a60}
header[data-shared] #btnHelp,header[data-shared] #btnHelp2{width:28px;height:26px;
  padding:0;display:flex;align-items:center;justify-content:center;
  font-size:14px;font-weight:700;line-height:1}
/* 右侧控件靠 .hdr-first 统一左推，这里不再用 :first-of-type 兜底 */
`;

  function currentPage() {
    const p = (location.pathname.split('/').pop() || 'index.html').toLowerCase();
    return p || 'index.html';
  }

  function build() {
    if (document.querySelector('header[data-shared]')) return;
    const page = currentPage();
    const h = document.createElement('header');
    h.setAttribute('data-shared', '1');

    // 让「资源管理」页的统计仍写到 #hdrStat；标题统一成超昂大战
    const subId = page === 'setup.html' ? 'hdrSub' : 'hdrSub';
    // 右侧控件：本页特有的在前，GitHub 图标固定放最后（三页都有）
    const right = (EXTRA[page] || []).map(k => CTRL[k]);
    right.push(GH_LINK);

    h.innerHTML =
      '<h1>超昂大战<small id="' + subId + '"></small></h1>' +
      '<nav>' + NAV.map(n =>
        '<a href="' + n.href + '"' + (n.href === page ? ' class="on"' : '') +
        '>' + n.text + '</a>').join('') + '</nav>' +
      right.join('');

    // 右侧第一个控件负责"把剩下所有东西推到右边"（各页控件数量不同，
    // 写在 CSS 里没法统一指定，所以交给 JS 标一下）
    const firstRight = h.querySelector('nav').nextElementSibling;
    if (firstRight) firstRight.classList.add('hdr-first');

    // 样式放到 head 末尾：与各页 css 同权重但顺序在后，能覆盖旧规则
    const st = document.createElement('style');
    st.textContent = CSS;
    document.head.appendChild(st);

    document.body.insertBefore(h, document.body.firstChild);

    // 原来各页的 <small id="cnt|hdCnt|ver"> 现在统一成 #hdrSub，做个别名
    ['cnt', 'hdCnt', 'ver'].forEach(function (id) {
      const old = document.getElementById(id);
      if (old) old.id = 'hdrSub';
    });
  }

  // **必须同步执行**，不能等 DOMContentLoaded：
  // 页面脚本（player.js / cg.js）在 body 末尾同步跑，它们的顶层代码会立刻去
  // 查 #hdrSub、#btnHelp 这些节点。等到 DOMContentLoaded 才建 header，
  // 那些查询会拿到 null（表现就是 "Cannot set properties of null"）。
  build();
})();
