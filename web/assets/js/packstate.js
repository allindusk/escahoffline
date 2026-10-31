/* 资源状态层：告诉播放器"这部剧情能不能播"
 *
 * 为什么单独一层
 * ─────────────
 * 播放器是纯静态页，可以被任何静态服务器打开（甚至 file://）。只有当作
 * 本地服务端（python -m server）在跑时，它才知道资源装没装。
 *
 * 本层策略：
 *   · 探测 /api/state，成功 → online = true，取到每个剧情的状态
 *       ready        硬依赖+软依赖都齐，完整可播
 *       soft-missing 缺公共背景/立绘/音乐，能播但会降级
 *       missing      缺剧情正文或语音，点了要引导去下载
 *   · 探测失败 → online = false，status 一律返回 'unknown'
 *     此时播放器行为与改造前完全一致（不显示任何标记、不拦截点击）
 *
 * 侧栏据此把"未下载"的剧情排到列表底部并打标记。
 */
window.PACKSTATE = (() => {
  const TTL = 15000;
  const cache = { at: 0, data: null, pub: null, online: false, promise: null };
  const listeners = [];

  function emit() {
    listeners.forEach(fn => { try { fn(cache.data); } catch (e) { console.warn(e); } });
  }

  async function refresh(force) {
    if (!force && cache.data && Date.now() - cache.at < TTL) return cache.data;
    if (cache.promise && !force) return cache.promise;
    cache.promise = (async () => {
      try {
        const [rs, rm, rp] = await Promise.all([
          fetch('api/state', { cache: 'no-store' }),
          cache.manifest ? Promise.resolve(null)
            : fetch('api/manifest', { cache: 'no-store' }).catch(() => null),
          // 公共素材进度（背景/BGM/音效/头像各下了多少）——侧栏头像要不要加载看它
          cache.pub ? Promise.resolve(null)
            : fetch('api/cdn/public', { cache: 'no-store' }).catch(() => null),
        ]);
        if (!rs.ok) throw new Error('HTTP ' + rs.status);
        const d = await rs.json();
        cache.online = true;
        cache.data = d;
        if (rm && rm.ok) cache.manifest = await rm.json();
        if (rp && rp.ok) cache.pub = await rp.json();
      } catch (e) {
        cache.online = false;
        cache.data = cache.data || null;
      }
      cache.at = Date.now();
      cache.promise = null;
      emit();
      return cache.data;
    })();
    return cache.promise;
  }

  /** 资源包安装状态：'ready' | 'soft-missing' | 'missing' | 'unknown' */
  function status(unit) {
    if (!cache.online || !cache.data) return 'unknown';
    return (cache.data.status || {})[unit] || 'missing';
  }

  function installed() {
    return (cache.data && cache.data.installed) || {};
  }

  /** 是否已经问过服务端（成功或失败都算）。
   *  页面首屏比 /api/state 早，这时 online 还是 false —— 不能把它当成
   *  "确定离线"，否则全新环境会先按"有图"渲染一轮，白打出几千个 404。 */
  function known() { return cache.at > 0; }

  function isInstalled(depot) {
    return !!installed()[depot];
  }

  function coverage() {
    return (cache.data && cache.data.coverage) || null;
  }

  function config() {
    return (cache.data && cache.data.config) || {};
  }

  /** 缺哪些包（给提示语用） */
  function missingDepots(unit) {
    const u = (cache.manifest && cache.manifest.units || {})[unit];
    const ins = installed();
    const hard = [], soft = [];
    if (!u) return { hard, soft };
    (u.depots || []).forEach(x => { if (!ins[x]) hard.push(x); });
    (u.soft || []).forEach(x => { if (!ins[x]) soft.push(x); });
    return { hard, soft };
  }

  function manifest() { return cache.manifest || null; }

  /** 公共素材计数：{back,backTotal,music,musicTotal,sfx,sfxTotal,avatar,avatarTotal} */
  function publicAssets() { return cache.pub || null; }

  /** 头像是否已随公共素材下载（≥98% 视为齐全）。侧栏判断要不要请求 <img> 用它，
   *  不能再用"剧情是否安装"——用户可能只装了 2 个角色却下了全部头像。 */
  function avatarsReady() {
    const p = cache.pub;
    if (!p) return false;
    const total = p.avatarTotal || 0;
    return total ? (p.avatar || 0) >= total * 0.98 : (p.avatar || 0) > 0;
  }

  /** 剧情横幅图（media/img/story/*.webp）是否基本齐全。
   *  侧栏每行第一眼画的就是横幅，没有它就别发请求（全新 / 只下了一小撮的
   *  环境里，按"有 1 张就有 384 张"去猜，只会换来满屏 404）。
   *  判定口径与头像一致：按已下载的横幅包数占总数比例（≥98%）算，
   *  而不是"磁盘上有没有图" —— 用户清过缓存后图还在、但包没了，照样该显示；
   *  反过来只下了一部分横幅时比例不够 → 整层不显示，避免个别文件 404。 */
  function bannersReady() {
    const p = cache.pub;
    if (!p) return false;
    const total = p.bannerTotal || 0;
    return total ? (p.banner || 0) >= total * 0.98
                : (p.bannerImg || 0) > 0;
  }

  /** 某剧情数据文件是否真的存在（正文在单元包里） */
  function hasStoryData(unit) {
    const st = status(unit);
    return st === 'ready' || st === 'soft-missing' || st === 'unknown';
  }

  function on(fn) { listeners.push(fn); }
  refresh();

  return {
    refresh, status, installed, isInstalled, coverage, config, known,
    missingDepots, hasStoryData, on, publicAssets, avatarsReady,
    bannersReady,
    get online() { return cache.online; },
    get data() { return cache.data; },
    human(n) {
      n = +n || 0;
      const u = ['B', 'KB', 'MB', 'GB', 'TB'];
      let i = 0;
      while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
      return (i === 0 ? n.toFixed(0) : n.toFixed(1)) + ' ' + u[i];
    },
  };
})();
