/* 运行状态提示条：① 程序更新后强制刷新 ② 路径含中文的警告
 *
 * 为什么需要"强制刷新"
 * -------------------
 * 更新是替换磁盘上的 server/ 与 web/assets/ 再重启服务。服务端立刻是新代码，
 * 但浏览器里已经跑着的页面还是旧 JS —— 不刷新会出现"服务端新增了接口、
 * 页面还在调旧接口"这种半新半旧的状态，表现就是按钮没反应。
 * 所以版本一变就在顶部挂一条提示，点了带 ?v=时间戳 重新加载（绕开缓存）。
 *
 * 为什么要在网页上提示路径
 * -----------------------
 * 启动时的控制台警告很多人根本看不到（窗口在后台）。路径含中文时，
 * 视频导出/图片编码这类外部组件容易出奇怪的错，提前说清楚省得排查半天。
 */
window.HEALTH = (() => {
  let knownVer = null;
  let warnedPath = false;
  let busy = false;
  // 已后台下载好、等用户点头的版本号；updateReady 变了才提示，避免每 60 秒弹一次
  let knownReady = '';
  let updating = false;

  const esc = (s) => String(s).replace(/[&<>"]/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  const reload = () => {
    const u = new URL(location.href);
    u.searchParams.set('v', Date.now());
    location.replace(u.toString());
  };

  async function post(path, body) {
    const r = await fetch('api/' + path, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}), cache: 'no-store',
    });
    if (!r.ok) throw new Error((await r.text()).slice(0, 140));
    return r.json();
  }

  // 点「重启并完成更新」→ 后台替换 + 重启 → 等它回来再提示刷新
  async function applyUpdate(ver) {
    if (updating) return;
    updating = true;
    show('正在更新到 <b>v' + esc(ver) + '</b>：服务即将重启替换文件（素材 / 译文 / 设置都不受影响）',
      null, null);
    try {
      await post('update/apply', {});
    } catch (e) {
      updating = false;
      show('更新失败：' + esc(e.message), '知道了', null, true);
      return;
    }
    const until = Date.now() + 180000;
    while (Date.now() < until) {
      await new Promise(r => setTimeout(r, 2000));
      try {
        const r = await fetch('api/state', { cache: 'no-store' });
        if (!r.ok) continue;
        const d = await r.json();
        if (d.version && d.version !== knownVer) {
          knownVer = d.version;
          updating = false;
          knownReady = '';
          show('更新完成，程序已是 <b>v' + esc(d.version) + '</b>，刷新页面即可生效',
            '刷新页面', reload);
          return;
        }
      } catch (e) { /* 重启中，继续等 */ }
    }
    updating = false;
    show('没等到程序自动回来。<br>请刷新页面；若仍是旧版本，双击 <code>启动.bat</code> 即可。',
      '刷新页面', () => location.reload());
  }

  const CSS = `
#hcBar{position:fixed;left:0;right:0;top:0;z-index:9600;display:flex;gap:12px;
  align-items:center;padding:9px 16px;font-size:13px;line-height:1.6;
  background:#3a2a0c;color:#ffe2b0;border-bottom:1px solid #6b4f14;
  box-shadow:0 4px 18px rgba(0,0,0,.45)}
#hcBar.warn{background:#3a1418;color:#ffc9cf;border-bottom-color:#6d2830}
#hcBar .sp{flex:1}
#hcBar button{border-radius:8px;padding:5px 14px;font-size:12.5px;cursor:pointer;
  border:1px solid #8a6a20;background:#5a4210;color:#ffe2b0}
#hcBar.warn button{border-color:#8d3a44;background:#5a1c22;color:#ffc9cf}
#hcBar .x{cursor:pointer;opacity:.6;padding:0 4px}
#hcBar .x:hover{opacity:1}
body.hc-on{padding-top:44px}
`;

  function style() {
    if (document.getElementById('hcCss')) return;
    const s = document.createElement('style');
    s.id = 'hcCss';
    s.textContent = CSS;
    document.head.appendChild(s);
  }

  function show(msg, btnText, onClick, isWarn) {
    style();
    let bar = document.getElementById('hcBar');
    if (!bar) {
      bar = document.createElement('div');
      bar.id = 'hcBar';
      document.body.appendChild(bar);
      document.body.classList.add('hc-on');
    }
    bar.className = isWarn ? 'warn' : '';
    bar.innerHTML = '';
    const txt = document.createElement('span');
    txt.innerHTML = msg;
    bar.appendChild(txt);
    bar.appendChild(Object.assign(document.createElement('span'), { className: 'sp' }));
    if (btnText) {
      const b = document.createElement('button');
      b.textContent = btnText;
      b.onclick = onClick || (() => bar.remove());
      bar.appendChild(b);
    }
    const x = document.createElement('span');
    x.className = 'x';
    x.textContent = '✕';
    x.title = '本次先关掉';
    x.onclick = () => { bar.remove(); document.body.classList.remove('hc-on'); };
    bar.appendChild(x);
  }

  async function tick() {
    if (busy) return;
    busy = true;
    try {
      const r = await fetch('api/state', { cache: 'no-store' });
      if (!r.ok) return;
      const d = await r.json();
      if (knownVer === null) {
        knownVer = d.version || '';
      } else if (d.version && d.version !== knownVer && !updating) {
        show('程序已更新到 <b>v' + esc(d.version) + '</b>，当前页面还是旧版本（新功能/修复还没生效）',
          '强制刷新', reload);
        knownVer = d.version;
      }
      // 后台已把新版本下好 → 挂条让用户决定什么时候重启（绝不自动重启）
      const ready = d.updateReady || '';
      if (ready && ready !== knownReady && !updating) {
        knownReady = ready;
        show('发现新版本 <b>v' + esc(ready) + '</b>，已经下好了。重启并完成更新即可生效'
          + '（<b>素材 / 译文 / 设置都不会动</b>）', '重启并完成更新',
          () => applyUpdate(ready));
      }
      if (d.warn && !warnedPath) {
        warnedPath = true;
        show(('⚠ ' + String(d.warn).split('\n')[0] +
          '　建议把项目挪到纯英文路径，否则视频导出可能失败。'), '知道了', null, true);
      }
    } catch (e) {
      /* 服务没起来 / 纯静态打开，静默 */
    } finally {
      busy = false;
    }
  }

  function start() {
    tick();
    setInterval(tick, 60000);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) tick(); });
  }

  return { start, show };
})();
