/* 全项目"会变的数字"的唯一入口（容量 / 耗时）。
 *
 * 数据来自 server/facts.py（GET api/facts），页面上**不写死数字**：
 *   静态文案  →  目录约 <span data-fact="catalog">…</span>
 *                加载后自动替换成 "5 MB"
 *   JS 拼串   →  FACT.t('public')      → "835 MB"
 *                 FACT.t('secUnit')    → "30–90 秒"
 *   算体积    →  FACT.unitsSize(n)     → n 部剧情约多少（"6.6 GB"）
 *
 * 为什么这么做：同一个容量以前在概览写 775 MB、在基础数据行写 835 MB，
 * 单位也是各写各的（有 MB 有 GB）。现在存的是字节，格式化只在这里做一次。
 */
(function () {
  'use strict';

  const KEY = 'escah_facts_v1';       // 上一次拿到的结果，用来"秒开"不闪数字
  const D = { bytes: {}, secs: {}, text: {}, parts: [] };
  let loading = null;

  /** 字节 → "5 MB" / "835 MB" / "20.8 GB"（整数不带小数点，和 Python 侧一致） */
  function sizeText(n) {
    n = +n || 0;
    const units = [['GB', 1024 * 1024 * 1024], ['MB', 1024 * 1024], ['KB', 1024]];
    for (const [u, step] of units) {
      if (n >= step) {
        const v = n / step;
        const t = (Math.round(v * 10) / 10).toString();
        return t + ' ' + u;
      }
    }
    return Math.round(n) + ' B';
  }

  function apply(d) {
    if (!d || !d.text) return false;
    D.bytes = d.bytes || {};
    D.secs = d.secs || {};
    D.text = d.text || {};
    D.parts = d.parts || [];
    return true;
  }

  /** 把页面上所有 [data-fact] 占位换成成品文案（没有值的保持原样） */
  function hydrate() {
    document.querySelectorAll('[data-fact]').forEach(function (el) {
      const v = D.text[el.dataset.fact];
      if (v) el.textContent = v;
    });
    document.querySelectorAll('[data-fact-title]').forEach(function (el) {
      const v = D.text[el.dataset.factTitle];
      if (v) el.title = v;
    });
    document.dispatchEvent(new CustomEvent('facts:ready'));
  }

  function load() {
    if (loading) return loading;
    loading = fetch('api/facts')
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (apply(d)) {
          try { localStorage.setItem(KEY, JSON.stringify(d)); } catch (e) { /* 隐私模式 */ }
          hydrate();
        }
        return D;
      })
      .catch(function () { return D; });       // 服务没起来：保持占位，不报错
    return loading;
  }

  // 先用上次缓存的数字把页面填上（避免"先看到 … 再跳成 835 MB"），再去取最新值
  try {
    const cached = localStorage.getItem(KEY);
    if (cached) apply(JSON.parse(cached));
  } catch (e) { /* 缓存坏了就当没有 */ }

  window.FACT = {
    ready: load,
    /** 成品文案，如 t('public') → "835 MB" */
    t: function (key, fallback) { return D.text[key] || fallback || ''; },
    /** 原始字节数（要自己算的时候用） */
    bytes: function (key) { return +(D.bytes[key] || 0); },
    /** 公共素材分项：[{key,label,bytes,text}] */
    parts: function () { return D.parts; },
    /** "背景 205 + BGM 音乐 539 + …" */
    breakdown: function () { return D.text.publicBreakdown || ''; },
    /** N 部剧情 ≈ 多少容量（按中间估值算；不会超过"全部素材"那个上限） */
    unitsSize: function (count) {
      const n = (+count || 0) * (D.bytes.unit || 0);
      const cap = D.bytes.allAssets || 0;
      return sizeText(cap ? Math.min(n, cap) : n);
    },
    sizeText: sizeText,
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', hydrate);
  } else {
    hydrate();
  }
  load();
})();
