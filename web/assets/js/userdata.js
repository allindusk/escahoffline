/* 收藏夹 + 播放进度（播放器页与 CG 页共用同一份存储）
 * ── 为什么单独一层 ────────────────────────────────────────────────
 * 收藏与"看到哪了"是**用户自己的数据**，跟剧情数据/资源包完全无关，
 * 两个页面都要读同一份（剧情页收藏的剧情、CG 页收藏的 CG 组），
 * 各写一遍必然出现"这页能收藏、那页看不见"的分裂。
 *
 * ── 存在哪（两层）────────────────────────────────────────────────
 * 1. 浏览器 localStorage（**主副本**，读写零延迟，两个页面天然共用）
 *      escah_fav   { story: { <favId>: 时间戳 }, cg: { <favId>: 时间戳 } }
 *      escah_prog  { <storyKey>: { p, mi, l, n, ts, t } }
 *      p=段（0 基） mi=该段的指令下标（跳转要用） l=第几句 n=共几句
 *      ts=最后一次看的时间 t=剧名快照（剧情数据没了也能显示）
 *      escah_prefs { set: {k:{v,ts}}, ui: {k:{v,ts}} }   界面偏好（见下）
 * 2. 本地文件（**备份**，由服务端读写；程序更新不会覆盖，也不联网）
 *      userdata/marks.json  ← /api/marks  收藏 + 播放进度
 *      userdata/prefs.json  ← /api/prefs  播放器设置 / 列表筛选 / 语言
 *      为什么要有它们：清浏览器数据 / 换浏览器 / 换电脑 / 手机扫码进来都是
 *      另一份 localStorage。打不开服务端（双击 html / 老版本没有这个接口）时
 *      自动退化成"只有本地"，行为与加这些功能之前完全一致。
 *
 * ── 删除怎么同步（墓碑）──────────────────────────────────────────
 * 收藏值可以是**负数**，表示"在这个时刻取消了收藏"；进度可以是 {del:1,ts}。
 * 没有墓碑的话，取消收藏在一台设备上删掉，另一台下次同步又把旧记录推回来，
 * 永远删不掉。比对规则统一：绝对值/时间戳大的赢（详见 server/marks.py）。
 *
 * ── 写盘策略 ─────────────────────────────────────────────────────
 * 播放时每显示一句都会记一次进度，逐句写 localStorage 太费（大剧本 900 句），
 * 所以内存里立即更新、落盘合并成 1.2s 一次，另有 pagehide/切后台兜底。
 * 收藏是点击行为，立即落盘、立即推服务端；进度最多 15s 推一次。
 *
 * 挂 window：本文件与 player.js / cg.js 都是 classic script，顶层同名 const
 * 会互相冲突（"already been declared"），统一走 window。
 */
window.UD = (() => {
  'use strict';

  const K_FAV = 'escah_fav';
  const K_PROG = 'escah_prog';
  const K_PREF = 'escah_prefs';
  const MAX_PROG = 400;          // 最多记这么多部剧情，超了丢最旧的
  const FLUSH_MS = 1200;         // 进度落 localStorage 的合并窗口
  const PUSH_MS = 15000;         // 进度推服务端的节流窗口
  const API = '/api/marks';
  const API_PREF = '/api/prefs';

  const now = () => Date.now();

  function readJSON(k, def) {
    try {
      const s = localStorage.getItem(k);
      return s ? JSON.parse(s) : def;
    } catch (e) { return def; }
  }
  function writeJSON(k, v) {
    try { localStorage.setItem(k, JSON.stringify(v)); return true; } catch (e) { return false; }
  }

  /* ---------- 收藏 ---------- */
  /* 值 = 毫秒时间戳；负数 = 取消收藏的时间（墓碑，同步删除要用） */
  const fav = { story: {}, cg: {} };
  (function loadFav() {
    const d = readJSON(K_FAV, null);
    if (!d || typeof d !== 'object') return;
    ['story', 'cg'].forEach(kind => {
      const src = d[kind];
      if (!src || typeof src !== 'object' || Array.isArray(src)) return;
      for (const id in src) {
        const v = Math.trunc(+src[id] || 0);
        if (id && v) fav[kind][id] = v;
      }
    });
  })();

  const isOn = v => (+v || 0) > 0;
  const subs = [];
  function emit(kind, id, on) {
    subs.forEach(fn => { try { fn(kind, id, on); } catch (e) { console.warn(e); } });
  }
  const saveFav = () => writeJSON(K_FAV, fav);

  /* ---------- 播放进度 ---------- */
  let prog = readJSON(K_PROG, null);
  if (!prog || typeof prog !== 'object' || Array.isArray(prog)) prog = {};
  // 字段兜底：老版本/手改过的记录可能缺项，读的时候统一成数字
  for (const k in prog) {
    const r = prog[k];
    if (!r || typeof r !== 'object') { delete prog[k]; continue; }
    r.ts = +r.ts || 0;
    if (r.del) { prog[k] = { del: 1, ts: r.ts }; continue; }
    r.p = +r.p || 0; r.mi = +r.mi || 0; r.l = +r.l || 0; r.n = +r.n || 0;
  }

  let dirty = false, timer = 0;
  function saveProg() {
    clearTimeout(timer); timer = 0; dirty = false;
    writeJSON(K_PROG, prog);
  }
  function markProg() {
    dirty = true;
    if (!timer) timer = setTimeout(saveProg, FLUSH_MS);
  }

  /** 记录条数上限：超出丢最旧的（不像收藏，播放记录是"最近看什么"的缓存） */
  function prune() {
    const keys = Object.keys(prog).filter(k => !prog[k].del);
    if (keys.length <= MAX_PROG) return;
    keys.sort((a, b) => (prog[a].ts || 0) - (prog[b].ts || 0));
    keys.slice(0, keys.length - MAX_PROG).forEach(k => { delete prog[k]; });
  }

  function pct(rec) {
    if (!rec || !rec.n) return 0;
    const done = Math.min(rec.l + 1, rec.n);
    return Math.max(0, Math.min(100, Math.round(done / rec.n * 100)));
  }

  /** 有效记录（墓碑 {del:1} 视为没有） */
  function progOf(key) {
    const r = key && prog[key];
    return (r && !r.del) ? r : null;
  }

  /* ================= 界面偏好（设置类数据） =================
   * 结构与服务端 userdata/prefs.json 一致：{ set: {k:{v,ts}}, ui: {k:{v,ts}} }
   *   set = 播放器「设置」面板的各项（音量/字号/对话框/排序…）
   *   ui  = 其余零散偏好（语言、分类、筛选、CG 页的排序/范围/卡片大小）
   * 逐键 LWW：谁后改谁赢。不需要墓碑 —— 设置永远有值（「恢复默认」写进去的
   * 就是默认值并拿到新时间戳，天然能传播）。
   *
   * 哪些**故意**不同步（是设备特性，不该跨设备）：
   *   escah_sidebar / escah_hist      侧栏开合（手机上侧栏是抽屉，默认收起）
   *   escah_disclaimer_v1 / tour_*    免责声明与新手引导，每台设备各自确认一次
   */
  const LEGACY_UI = {              // ui 命名空间 ↔ 老 localStorage 键（一次性迁移用）
    src: 'escah_src', dlf: 'escah_dlf', lang: 'escah_lang',
    cgSort: 'escah_cg_sort', cgDl: 'escah_cg_dl',
    cgScope: 'escah_cg_scope', cgSize: 'escah_cg_size',
  };
  const prefs = (function () {
    const d = readJSON(K_PREF, null);
    const out = { set: {}, ui: {} };
    if (d && typeof d === 'object') {
      ['set', 'ui'].forEach(ns => {
        const src = d[ns];
        if (!src || typeof src !== 'object' || Array.isArray(src)) return;
        for (const k in src) {
          const e = src[k];
          if (!e || typeof e !== 'object' || !('v' in e)) continue;
          out[ns][k] = { v: e.v, ts: +e.ts || 0 };
        }
      });
    }
    /* 老版本没有 prefs：把现有 localStorage 里的偏好登记进来，时间戳给 1 ——
       这样"别的设备改过的值"（时间戳是真实时间）永远优先，新设备不会反过来
       把老设备的偏好覆盖成默认值。 */
    for (const k in LEGACY_UI) {
      if (out.ui[k]) continue;
      let v = null;
      try { v = localStorage.getItem(LEGACY_UI[k]); } catch (e) { }
      if (v != null && v !== '') out.ui[k] = { v, ts: 1 };
    }
    return out;
  })();
  const savePref = () => writeJSON(K_PREF, prefs);
  let setSnap = null;              // syncSet 的 diff 基准
  // 迁移结果落一次盘，免得每次打开都重新从老键读一遍
  if (!readJSON(K_PREF, null)
      && (Object.keys(prefs.ui).length || Object.keys(prefs.set).length)) savePref();

  function prefValue(ns, key, def) {
    const e = (prefs[ns] || {})[key];
    return e ? e.v : def;
  }

  /** 写一个键；值没变返回 false。传了 ts 就当"远端来的"，更旧的不覆盖。 */
  function prefWrite(ns, key, v, ts) {
    const dst = prefs[ns] || (prefs[ns] = {});
    const cur = dst[key];
    const t = (ts == null) ? now() : +ts;
    if (ts == null && cur && cur.v === v) return false;
    if (cur && (+cur.ts || 0) > t) return false;
    if (cur && (+cur.ts || 0) === t && cur.v === v) return false;
    dst[key] = { v, ts: t };
    return true;
  }

  /** 用户改了一个偏好（下拉 / 滑块 / 按钮） */
  function syncPref(ns, key, v) {
    if (!key) return;
    if (!prefWrite(ns, key, v)) return;
    savePref();
    markPrefSync();
  }

  /** 播放器设置整份：只给"确实变了的键"打时间戳（拖一次滑块只动一个键）。
   *  seed=true 时把本机已有、prefs 里还缺的键补齐（老用户第一次升级）。 */
  function syncSet(obj, seed) {
    if (!obj || typeof obj !== 'object') return;
    if (!setSnap) setSnap = Object.assign({}, obj);
    let n = 0;
    for (const k in obj) {
      const missing = !!(seed && !prefs.set[k]);
      if (!missing && setSnap[k] === obj[k]) continue;
      setSnap[k] = obj[k];
      if (missing ? prefWrite('set', k, obj[k], 1) : prefWrite('set', k, obj[k])) n++;
    }
    if (n) { savePref(); markPrefSync(); }
  }

  /** 把服务端 / 别的设备的偏好并进本机（逐键 LWW），返回是否有变化 */
  function applyRemotePrefs(remote) {
    if (!remote || typeof remote !== 'object') return false;
    let changed = false;
    ['set', 'ui'].forEach(ns => {
      const src = remote[ns];
      if (!src || typeof src !== 'object' || Array.isArray(src)) return;
      const dst = prefs[ns] || (prefs[ns] = {});
      for (const k in src) {
        const e = src[k];
        if (!e || typeof e !== 'object' || !('v' in e)) continue;
        const t = +e.ts || 0;
        if (dst[k] && (+dst[k].ts || 0) >= t) continue;
        dst[k] = { v: e.v, ts: t };
        changed = true;
      }
    });
    // 基准作废：下一次 syncSet 以"合并后的值"为准，不会把远端值当成用户刚改的
    if (changed) { savePref(); setSnap = null; }
    return changed;
  }

  /* ================= 与服务端的备份同步 =================
   * localStorage 是主，userdata/marks.json 是备份：启动拉一次合并，之后改动推回去。
   * 服务端也会合并（多端/多标签页各自推整份也不会互相删掉），返回合并后的结果，
   * 我们再并一次 → 三边最终一致。 */
  let pushTimer = 0, dirtySync = false, hydrating = null, offline = false, lastPush = 0;

  function payload() { return { schema: 1, fav, prog }; }

  /** 把服务端那份并进内存，返回是否有变化 */
  function mergeRemote(remote) {
    if (!remote || typeof remote !== 'object') return false;
    let changed = false;
    const rf = remote.fav;
    if (rf && typeof rf === 'object') {
      ['story', 'cg'].forEach(kind => {
        const src = rf[kind];
        if (!src || typeof src !== 'object' || Array.isArray(src)) return;
        const dst = fav[kind] || (fav[kind] = {});
        for (const id in src) {
          const v = Math.trunc(+src[id] || 0);
          if (!id || !v) continue;
          if (Math.abs(v) > Math.abs(+dst[id] || 0)) { dst[id] = v; changed = true; }
        }
      });
    }
    const rp = remote.prog;
    if (rp && typeof rp === 'object' && !Array.isArray(rp)) {
      for (const key in rp) {
        const r = rp[key];
        if (!key || !r || typeof r !== 'object') continue;
        const ts = Math.trunc(+r.ts || 0);
        if (!ts) continue;
        const cur = prog[key];
        if (cur && (+cur.ts || 0) >= ts) continue;
        prog[key] = r.del ? { del: 1, ts } : {
          p: +r.p || 0, mi: +r.mi || 0, l: +r.l || 0,
          n: +r.n || 0, ts, t: String(r.t || '').slice(0, 120),
        };
        changed = true;
      }
      prune();
    }
    return changed;
  }

  function pushNow() {
    clearTimeout(pushTimer); pushTimer = 0;
    dirtySync = false;
    if (offline) return Promise.resolve(false);
    lastPush = now();
    return fetch(API, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload()),
    }).then(r => (r.ok ? r.json() : null)).then(res => {
      if (res && res.marks && mergeRemote(res.marks)) {
        saveFav(); saveProg();
      }
      return true;
    }).catch(() => { offline = true; return false; });
  }

  /** 一次改动后：马上推（收藏/删除/清空）或攒一会儿（每句进度）。
   *
   *  "马上"也给 150ms 合并窗口：连点几颗星只发一次 POST。
   *  真并发两个 POST 时服务端虽然加了锁，但两边各自把整份状态写回仍是浪费，
   *  而且旧实现里后发的那份可能覆盖先发的（实测丢过一条收藏）。
   */
  function markSync(immediate) {
    dirtySync = true;
    if (pushTimer) {
      if (!immediate) return;          // 已经在排队了，到点会一起推
      clearTimeout(pushTimer); pushTimer = 0;
    }
    const wait = immediate ? 150 : Math.max(0, PUSH_MS - (now() - lastPush));
    pushTimer = setTimeout(pushNow, wait);
  }

  /* ---------- 偏好的推送（拖滑块会连发，攒 400ms 一次） ---------- */
  let prefTimer = 0, prefDirty = false;
  function markPrefSync() {
    prefDirty = true;
    if (prefTimer) return;
    prefTimer = setTimeout(pushPrefs, 400);
  }
  function pushPrefs() {
    clearTimeout(prefTimer); prefTimer = 0;
    prefDirty = false;
    if (offline) return Promise.resolve(false);
    return fetch(API_PREF, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(prefs),
    }).then(r => (r.ok ? r.json() : null)).then(res => {
      // 服务端可能并了别的设备的值：拿回来再并一次（不会覆盖本机更新的键）
      if (res && res.prefs) applyRemotePrefs(res.prefs);
      return true;
    }).catch(() => { offline = true; return false; });
  }

  /** 启动时拉一次：把备份里的收藏/进度/偏好并回本机。
   *  返回 { marks, prefs } —— 各自是否带来了新数据，调用方据此重画界面。 */
  function hydrate() {
    if (hydrating) return hydrating;
    const one = url => fetch(url, { cache: 'no-store' })
      .then(r => (r.ok ? r.json() : null)).catch(() => null);
    hydrating = Promise.all([one(API), one(API_PREF)])
      .then(([remoteMarks, remotePrefs]) => {
        const marksNew = mergeRemote(remoteMarks);
        if (marksNew) { saveFav(); saveProg(); }
        const prefsNew = applyRemotePrefs(remotePrefs);
        // 无条件推一次：老用户（本地攒了一堆收藏/设置、服务端还没有文件）第一次
        // 打开时，靠这一下把本地数据落盘成备份，不然要等到下一次改动才写文件。
        pushNow();
        pushPrefs();
        return { marks: marksNew, prefs: prefsNew };
      })
      .catch(() => { offline = true; return { marks: false, prefs: false }; });
    return hydrating;
  }

  window.addEventListener('pagehide', () => {
    if (dirty) saveProg();
    if (offline) return;
    try {
      // 关页面时普通 fetch 会被掐断，用 sendBeacon 把最后一次改动送出去
      if (dirtySync) {
        navigator.sendBeacon(API,
          new Blob([JSON.stringify(payload())], { type: 'application/json' }));
      }
      if (prefDirty) {
        navigator.sendBeacon(API_PREF,
          new Blob([JSON.stringify(prefs)], { type: 'application/json' }));
      }
    } catch (e) { /* 老浏览器没有 sendBeacon：放弃这一次，本地已经存好了 */ }
  });
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) return;
    if (dirty) saveProg();
    if (dirtySync) pushNow();
    if (prefDirty) pushPrefs();
  });

  return {
    /* --- 收藏 --- */
    isFav(kind, id) { return !!(id && isOn((fav[kind] || {})[id])); },
    favs(kind) {
      const m = fav[kind] || {}, out = {};
      for (const id in m) if (isOn(m[id])) out[id] = m[id];
      return out;
    },
    favCount(kind) {
      const m = fav[kind] || {};
      return Object.keys(m).filter(id => isOn(m[id])).length;
    },
    /** 切换收藏，返回新状态（true=已收藏） */
    toggleFav(kind, id) {
      if (!id) return false;
      const m = fav[kind] || (fav[kind] = {});
      const on = !isOn(m[id]);
      m[id] = on ? now() : -now();       // 负值 = 取消，同步删除要用（墓碑）
      saveFav();
      markSync(true);
      emit(kind, id, on);
      return on;
    },
    /** 订阅收藏变化（同页多个视图共用一份状态时要重画） */
    onFav(fn) { if (typeof fn === 'function') subs.push(fn); },

    /* --- 播放进度 --- */
    prog: progOf,
    /** 记一句：part=段(0基) mi=指令下标 line=第几句 total=本段共几句 */
    setProg(key, part, mi, line, total, title) {
      if (!key) return;
      const old = progOf(key);
      const p = Math.max(0, part | 0);
      const i = Math.max(0, mi | 0);
      const l = Math.max(0, line | 0);
      // 同一句反复进来（跳转重放/暂停继续）不刷新时间戳，免得"最近播放"乱跳
      if (old && old.p === p && old.mi === i && old.l === l) {
        if (title && !old.t) { old.t = String(title).slice(0, 120); markProg(); }
        return;
      }
      const rec = { p, mi: i, l, n: Math.max(0, total | 0), ts: now() };
      rec.t = String(title || (old && old.t) || '').slice(0, 120);
      prog[key] = rec;
      prune();
      markProg();
      markSync(false);
    },
    clearProg(key) {
      if (!key || !progOf(key)) return;
      prog[key] = { del: 1, ts: now() };    // 墓碑：否则下次同步会把这条推回来
      markProg();
      markSync(true);
    },
    clearAllProg() {
      const ts = now();
      Object.keys(prog).forEach(k => { if (!prog[k].del) prog[k] = { del: 1, ts }; });
      markProg();
      markSync(true);
    },
    /** 全部记录，按最后观看时间倒序 */
    recent() {
      return Object.keys(prog)
        .filter(k => !prog[k].del)
        .map(k => Object.assign({ key: k }, prog[k]))
        .sort((a, b) => (b.ts || 0) - (a.ts || 0));
    },
    progPct: pct,
    /** 上次看到第几句（1 基，给人看的） */
    lineNo(rec) { return rec ? (rec.l || 0) + 1 : 0; },
    ago(ts) {
      const t = +ts || 0;
      if (!t) return '';
      const sec = Math.max(0, Date.now() - t) / 1000;
      if (sec < 60) return '刚刚';
      if (sec < 3600) return Math.floor(sec / 60) + ' 分钟前';
      if (sec < 86400) return Math.floor(sec / 3600) + ' 小时前';
      if (sec < 86400 * 7) return Math.floor(sec / 86400) + ' 天前';
      const d = new Date(t);
      const p2 = n => String(n).padStart(2, '0');
      return `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())}`;
    },

    /* --- 界面偏好 --- */
    prefValue,
    syncPref,
    syncSet,
    /** 服务端/别的设备的偏好并进来（hydrate 已经做，个别页面要手动同步时用） */
    applyRemotePrefs,
    /** 立刻把偏好推上去（测试/调试用） */
    pushPrefs,

    /* --- 同步 --- */
    hydrate,
    /** 立刻把当前状态推给服务端（测试/调试用） */
    push: pushNow,
    /** 还有没推出去的改动？ */
    pendingSync() { return dirtySync || prefDirty; },
    flush() {
      if (dirty) saveProg();
      if (dirtySync) pushNow();
      if (prefDirty) pushPrefs();
    },
  };
})();
