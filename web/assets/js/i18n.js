/* 多语言层（i18n）
 * ── 为什么单独拆一层 ──────────────────────────────────────────────
 * 剧情数据 char_<id>.js 是从资源包「再生」出来的产物，重解一次就整体重写。
 * 译文必须与之分离，否则任何一次剧本重提取都会把手头成果冲掉。
 *
 * ── 约定 ─────────────────────────────────────────────────────────
 *   剧情译文  assets/i18n/<lang>/char_<id>.json  { messages: { "<日文原文>": "<译文>" } }
 *   界面文案  assets/i18n/ui.json                { messages: { <lang>: { key: text } } }
 *   译文清单  assets/i18n/index.json             { chars: { <id>: { <lang>: 条数 } } }
 *
 * ── 核心策略 ─────────────────────────────────────────────────────
 *   译文 key 用「日文原文」而不是「第几段第几句」的下标。
 *   下标会随台词增删整体错位，把 A 的译文贴到 B 上；原文做 key 时上游一变就
 *   自然失配，干净地落到「回退日文」，绝不错配。
 *
 * ── 降级原则 ─────────────────────────────────────────────────────
 *   任何加载失败（文件缺失 / file:// 协议 / 断网）都只是"没有译文"，
 *   一律回退显示日文原文 —— 页面行为与加这层之前完全一致。
 *
 * 挂到 window 上：本文件与 player.js 是两个 classic script，若各自用顶层
 * const 声明同名标识符会冲突（"already been declared"），统一走 window。
 */
window.I18N = (() => {
  const KEY_STORE = 'escah_lang';
  const SOURCE = 'ja';
  const SUPPORTED = ['zh-Hans', 'ja'];      // 顺序即切换按钮的轮换顺序（中文优先）
  const DEFAULT_LANG = 'zh-Hans';           // 首次访问默认中文；未译文句自动回退日文
  const LABEL = { ja: '日本語', 'zh-Hans': '中文' };

  let lang = SOURCE;
  let uiReady = Promise.resolve();
  const ui = {};                            // lang -> {key: text}
  const story = new Map();                  // "lang|cid" -> Map(ja -> 译文)
  const wanted = new Map();                 // "lang|cid" -> Promise
  const listeners = [];

  const norm = v => (SUPPORTED.includes(v) ? v : SOURCE);

  function readLang() {
    const q = new URLSearchParams(location.search).get('lang');
    if (q) return norm(q);                  // ?lang=xx 测试钩子，优先级最高
    try {
      const s = localStorage.getItem(KEY_STORE);
      if (s) return norm(s);                // 用户手动切过则以记忆为准
    } catch (e) { }
    return DEFAULT_LANG;                    // 首次访问默认中文
  }

  async function loadJson(url) {
    const r = await fetch(url, { cache: 'no-cache' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }

  /* ---------- 界面文案 ---------- */
  async function loadUi() {
    try {
      const d = await loadJson('assets/i18n/ui.json');
      const msgs = d.messages || {};
      for (const L of SUPPORTED) ui[L] = msgs[L] || {};
    } catch (e) {
      for (const L of SUPPORTED) ui[L] = ui[L] || {};
      console.warn('[i18n] ui.json 未加载，界面保持原语言：', e.message);
    }
  }

  /** 取界面文案；缺 key 时用 fallback（通常传 HTML 里的现有文字）。 */
  function t(key, fallback, vars) {
    let s = (ui[lang] || {})[key];
    if (s == null && lang !== SOURCE) s = (ui[SOURCE] || {})[key];
    if (s == null) s = fallback != null ? fallback : key;
    if (vars) for (const k in vars) s = String(s).replace('{' + k + '}', vars[k]);
    return s;
  }

  /* 把译文写进 DOM：只动「第一个文本节点」，保住 <input>/<span> 等子元素 */
  function setNodeText(el, text) {
    if (el.firstChild && el.firstChild.nodeType === 3) {
      el.firstChild.nodeValue = text;
    } else {
      el.textContent = text;
    }
  }

  function applyDom() {
    document.querySelectorAll('[data-i18n]').forEach(el => {
      const def = el.getAttribute('data-i18n-default');
      if (def == null) el.setAttribute('data-i18n-default', el.textContent.trim());
      const fb = def != null ? def : el.textContent.trim();
      setNodeText(el, t(el.getAttribute('data-i18n'), fb));
    });
    document.querySelectorAll('[data-i18n-title]').forEach(el => {
      const key = el.getAttribute('data-i18n-title');
      if (el.getAttribute('data-i18n-title-default') == null) {
        el.setAttribute('data-i18n-title-default', el.getAttribute('title') || '');
      }
      const fb = el.getAttribute('data-i18n-title-default');
      const s = t(key, fb);
      if (s) el.setAttribute('title', s);
    });
    document.querySelectorAll('[data-i18n-ph]').forEach(el => {
      if (el.getAttribute('data-i18n-ph-default') == null) {
        el.setAttribute('data-i18n-ph-default', el.getAttribute('placeholder') || '');
      }
      el.setAttribute('placeholder',
        t(el.getAttribute('data-i18n-ph'), el.getAttribute('data-i18n-ph-default')));
    });
  }

  /* ---------- 剧情译文 ---------- */
  /* key 是「剧情标识」而不是角色 ID：角色剧情为 char_<cid>，
     主线/活动/其他为 adv_<kind>_<no>。角色沿用原文件名 char_<id>.json，
     所以对角色剧情而言行为与改造前完全一致（译文仓库无需迁移）。 */
  function key(storyKey) { return lang + '|' + storyKey; }

  function storyFor(storyKey) {
    if (lang === SOURCE) return null;       // 源语言直接用原文，不需要查表
    return story.get(key(storyKey)) || null;
  }

  /** 预加载某部剧情的译文。不阻塞调用方：失败也只是没有译文。 */
  function loadStory(storyKey) {
    if (lang === SOURCE) return Promise.resolve(null);
    const k = key(storyKey);
    if (story.has(k)) return Promise.resolve(story.get(k));
    if (wanted.get(k)) return wanted.get(k);
    const p = loadJson(`assets/i18n/${lang}/${storyKey}.json`)
      .then(d => {
        const m = new Map(Object.entries(d.messages || {}));
        story.set(k, m);
        wanted.delete(k);
        return m;
      })
      .catch(() => { wanted.delete(k); story.set(k, new Map()); return null; });
    wanted.set(k, p);
    return p;
  }

  /** 取一句台词：优先译文，没有就回退源文本。永不返回 null。 */
  function msg(m, field) {
    const src = field || 'Message';
    const ja = m == null ? '' : (m[src] != null ? m[src] : '');
    if (lang === SOURCE) return ja;
    const map = story.get(key(ST_CHAR()));
    if (map) {
      const hit = map.get(ja);
      if (hit != null && hit !== '') return hit;
    }
    return ja;
  }

  /** 当前播放的角色 id（由 player.js 注入，避免在窗口全局变量上乱翻）。 */
  let _charFn = () => null;
  function ST_CHAR() { try { return _charFn(); } catch (e) { return null; } }

  /* ---------- 语言切换 ---------- */
  function emit() {
    listeners.forEach(fn => { try { fn(lang); } catch (e) { console.warn(e); } });
  }

  function setLang(next, silent) {
    next = norm(next);
    if (next === lang) return lang;
    lang = next;
    try { localStorage.setItem(KEY_STORE, lang); } catch (e) { }
    applyDom();
    if (!silent) emit();
    return lang;
  }

  function cycle() {
    const i = SUPPORTED.indexOf(lang);
    return setLang(SUPPORTED[(i + 1) % SUPPORTED.length]);
  }

  /* ---------- 启动 ---------- */
  lang = readLang();
  uiReady = loadUi().then(() => applyDom());

  return {
    lang: () => lang,
    label: () => LABEL[lang] || lang,
    source: SOURCE,
    supported: SUPPORTED,
    ready: uiReady,
    t, setLang, cycle, applyDom,
    loadStory, storyFor, msg,
    hasDict: storyKey => (lang !== SOURCE) && story.has(key(storyKey)),
    on(fn) { listeners.push(fn); },
    bindChar(fn) { _charFn = fn; },
  };
})();
