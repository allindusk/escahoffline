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
header[data-shared] #hdrStat{margin-left:auto;font-size:11.5px;color:#6b7488}
header[data-shared] button{flex:none;padding:3px 10px;border-radius:8px;
  font-size:12px;line-height:1.4;cursor:pointer;color:#9aa4b8;background:#1c2230;
  border:1px solid #2a3040;font-family:inherit}
header[data-shared] button:hover{color:#e6e9f0;border-color:#3a4a60}
header[data-shared] #btnHelp,header[data-shared] #btnHelp2{width:28px;height:26px;
  padding:0;display:flex;align-items:center;justify-content:center;
  font-size:14px;font-weight:700;line-height:1}
/* 没有 #hdrStat 的页面，让按钮自己靠右 */
header[data-shared] > button:first-of-type{margin-left:auto}
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
    h.innerHTML =
      '<h1>超昂大战<small id="' + subId + '"></small></h1>' +
      '<nav>' + NAV.map(n =>
        '<a href="' + n.href + '"' + (n.href === page ? ' class="on"' : '') +
        '>' + n.text + '</a>').join('') + '</nav>' +
      (EXTRA[page] || []).map(k => CTRL[k]).join('');

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
