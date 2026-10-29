/* 新手引导：逐个高亮 + 一句话说明
 *
 * 为什么不做成"点到哪弹哪"
 * ──────────────────────
 * 纯靠用户自己探索，小白根本不知道该点哪里；纯靠被动弹窗又会烦人。
 * 折中方案（也是大多数成熟软件的做法）：
 *   · 首次访问 → 弹一张"欢迎卡"，给出 3 个去处（教程 / 直接玩 / 下载资源）
 *   · 用户点"带我认识界面" → 逐步骤高亮+说明，←/→ 翻页，Esc 随时退出
 *   · 之后随时可点右上角「?」重新看；也可以按 H
 *   · 教程状态记在 localStorage，不再打扰
 *
 * 任何页面都能用：调用 TOUR.start(STEPS) 即可，STEPS 由页面自己定义。
 */
window.TOUR = (() => {
  const KEY = 'escah_tour_v1';
  let st = null;

  function css() {
    if (document.getElementById('tourCss')) return;
    const s = document.createElement('style');
    s.id = 'tourCss';
    s.textContent = `
#tourMask{position:fixed;inset:0;z-index:9998;pointer-events:none}
#tourMask .spot{position:absolute;border-radius:10px;
  box-shadow:0 0 0 9999px rgba(0,0,0,.72), 0 0 0 2px rgba(122,220,255,.9) inset;
  transition:all .22s ease}
#tourTip{position:fixed;z-index:9999;max-width:min(400px,86vw);
  background:#111a28;color:#e8f1ff;border:1px solid #2b4a6b;border-radius:12px;
  padding:14px 16px;box-shadow:0 10px 40px rgba(0,0,0,.6);font-size:13.5px;
  line-height:1.75}
#tourTip h4{margin:0 0 6px;font-size:14.5px;color:#7adcff}
#tourTip p{margin:0 0 10px;color:#cfe2f7}
#tourTip .tfoot{display:flex;align-items:center;gap:8px}
#tourTip .tfoot .idx{color:#7d92ab;font-size:12px;margin-right:auto}
#tourTip button{background:#1d3550;color:#dff0ff;border:1px solid #2f5478;
  border-radius:8px;padding:5px 12px;font-size:12.5px;cursor:pointer}
#tourTip button.primary{background:#2b6ea8;border-color:#3f8dc9}
#tourTip button:hover{filter:brightness(1.2)}
#tourWelcome{position:fixed;inset:0;z-index:9997;background:rgba(4,8,14,.86);
  display:flex;align-items:center;justify-content:center}
#tourWelcome .wcard{width:min(560px,92vw);background:#0e1622;border:1px solid #26405c;
  border-radius:16px;padding:26px 28px;color:#dbe9f8;box-shadow:0 20px 70px #000}
#tourWelcome h3{margin:0 0 8px;font-size:19px;color:#7adcff}
#tourWelcome p{margin:0 0 14px;color:#a9c0d8;font-size:13.5px;line-height:1.85}
#tourWelcome ul{margin:0 0 18px;padding-left:20px;color:#c7d9ec;font-size:13px;line-height:1.9}
#tourWelcome .wbtns{display:flex;gap:10px;flex-wrap:wrap}
#tourWelcome button{flex:1;min-width:130px;padding:10px 14px;border-radius:10px;
  border:1px solid #2f5478;background:#16283c;color:#dff0ff;font-size:13px;cursor:pointer}
#tourWelcome button.primary{background:#2b6ea8;border-color:#4b96d4;font-weight:600}
#tourWelcome button:hover{filter:brightness(1.18)}
@media (max-width:820px){#tourWelcome .wbtns{flex-direction:column}}
`;
    document.head.appendChild(s);
  }

  function clear() {
    document.querySelectorAll('#tourMask,#tourTip').forEach(e => e.remove());
    st = null;
  }

  function place(el) {
    const r = el.getBoundingClientRect();
    const spot = document.querySelector('#tourMask .spot');
    spot.style.left = (r.left - 6) + 'px';
    spot.style.top = (r.top - 6) + 'px';
    spot.style.width = (r.width + 12) + 'px';
    spot.style.height = (r.height + 12) + 'px';
    return r;
  }

  function tipFor(step, r, i, n) {
    const tip = document.querySelector('#tourTip');
    tip.innerHTML =
      `<h4>${step.title}</h4><p>${step.text}</p>` +
      // 必须有"跳过"：用户点开教程却只能一步步走完，这体验很糟
      `<div class="tfoot"><span class="idx">${i + 1} / ${n}</span>` +
      `<button id="tourSkip" title="关闭引导（Esc）">跳过</button>` +
      `<button id="tourPrev">上一步</button>` +
      `<button id="tourNext" class="primary">${i + 1 === n ? '开始使用' : '下一步'}</button></div>`;
    const tw = Math.min(400, window.innerWidth * 0.86);
    tip.style.width = tw + 'px';
    const th = tip.offsetHeight || 120;
    let left = Math.min(Math.max(12, r.left), window.innerWidth - tw - 12);
    let top = r.bottom + 14;
    if (top + th > window.innerHeight - 12) top = Math.max(12, r.top - th - 14);
    tip.style.left = left + 'px';
    tip.style.top = top + 'px';
    tip.querySelector('#tourNext').onclick = () => go(i + 1);
    tip.querySelector('#tourPrev').onclick = () => go(i - 1);
    const sk = tip.querySelector('#tourSkip');
    if (sk) sk.onclick = () => done();
  }

  function go(i) {
    if (!st) return;
    const steps = st.steps;
    // 跳过找不到的元素（页面结构变了也不崩）
    while (i < steps.length && !document.querySelector(steps[i].el)) i++;
    if (i >= steps.length) { done(); return; }
    st.i = i;
    const step = steps[i];
    if (step.before) { try { step.before(); } catch (e) { } }
    const el = document.querySelector(step.el);
    try { el.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); } catch (e) { }
    const r = place(el);
    tipFor(step, r, i, steps.length);
  }

  function start(steps, opt) {
    if (!steps || !steps.length) return;
    if (st) clear();
    css();
    const mask = document.createElement('div');
    mask.id = 'tourMask';
    mask.innerHTML = '<div class="spot"></div>';
    document.body.appendChild(mask);
    const tip = document.createElement('div');
    tip.id = 'tourTip';
    document.body.appendChild(tip);
    st = { steps, i: 0, key: (opt && opt.key) || KEY };
    go(0);
    window.addEventListener('resize', onResize);
  }

  let rzTimer = 0;
  function onResize() {
    clearTimeout(rzTimer);
    rzTimer = setTimeout(() => { if (st) go(st.i); }, 160);
  }

  function done() {
    if (st) {
      try { localStorage.setItem(st.key, '1'); } catch (e) { }
    }
    clear();
    window.removeEventListener('resize', onResize);
    document.dispatchEvent(new CustomEvent('tour:done'));
  }

  function welcome(opt) {
    css();
    const w = document.createElement('div');
    w.id = 'tourWelcome';
    w.innerHTML = `
<div class="wcard">
  <h3>${opt.title || '欢迎使用 · 超昂大战 角色剧情离线阅读器'}</h3>
  <p>${opt.intro || '这是一个把游戏剧情做成「可离线翻阅的电子书」的工具。第一次用建议先花 1 分钟认识界面。'}</p>
  <ul>
    <li><b>先装资源</b>：默认不带资源包（体积大 / 版权原因），在「资源管理」里下载 <code>核心 + 公共素材 + 想看的角色</code>。</li>
    <li><b>再看剧情</b>：左侧选角色 → 点播放，自动演出并带中文译文与语音。</li>
    <li><b>还能导出</b>：本段剧情录成视频、角色 CG 打包下载、译文导出给 AI 翻译再导回。</li>
  </ul>
  <div class="wbtns">
    <button class="primary" id="wTour">带我认识界面</button>
    <button id="wGo">直接开始</button>
    <button id="wSetup">去下载资源</button>
  </div>
</div>`;
    document.body.appendChild(w);
    const close = () => { w.remove(); try { localStorage.setItem(KEY, '1'); } catch (e) { } };
    w.querySelector('#wTour').onclick = () => { close(); start(opt.steps, opt); };
    w.querySelector('#wGo').onclick = close;
    w.querySelector('#wSetup').onclick = () => { close(); location.href = 'setup.html'; };
  }

  function auto(opt) {
    // ?notour=1 供自动化截图/测试跳过引导
    try {
      if (new URLSearchParams(location.search).has('notour')) return;
    } catch (e) { }
    let seen = '1';
    try { seen = localStorage.getItem(opt.key || KEY); } catch (e) { }
    if (!seen) welcome(opt);
  }

  document.addEventListener('keydown', e => {
    if (!st) return;
    if (e.key === 'Escape') { done(); }
    else if (e.key === 'ArrowRight') { go(st.i + 1); }
    else if (e.key === 'ArrowLeft') { go(st.i - 1); }
  });

  return { start, welcome, auto, done, get active() { return !!st; } };
})();
