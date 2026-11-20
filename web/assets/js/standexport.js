/* 立绘页的「导出立绘」按钮
 *
 * 和 CG 页的「导出 CG」同一套做法：打包在服务端做（浏览器没有 zip 能力，
 * 也不为一个按钮引第三方库），这里只负责算清楚"要导哪些组" → POST
 * /api/export/stand → 轮询任务进度。
 *
 * 导什么 = 页面上当前看得见的卡（`STANDPAGE.exportGroups()`）：
 *   · 左侧点了某个角色 → 只导这个角色的全部服装；
 *   · 没点（「全部」） → 导当前范围的全部（角色立绘 / 其他立绘）；
 *   · 勾了「只看已下载 / 只看已收藏」 → 只导筛选后的那些。
 * 立绘优先导高清图（服务端优先取 imgmap.x2 指过去的那份）。
 */
(() => {
  const $ = s => document.querySelector(s);

  function toast(msg, bad) {
    let el = $('#standToast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'standToast';
      el.style.cssText =
        'position:fixed;left:50%;top:64px;transform:translateX(-50%);z-index:9800;' +
        'padding:9px 18px;border-radius:10px;font-size:14.6px;transition:opacity .3s;' +
        'box-shadow:0 10px 30px rgba(0,0,0,.5);background:#16283c;border:1px solid #2f5478;color:#dff0ff';
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.style.borderColor = bad ? '#a33' : '#2f5478';
    el.style.color = bad ? '#ffb3ba' : '#dff0ff';
    el.style.opacity = '1';
    clearTimeout(el._t);
    el._t = setTimeout(() => { el.style.opacity = '0'; }, 3600);
  }

  const btn = $('#btnExportStand');
  if (!btn) return;
  const LABEL = btn.textContent;

  /* 立绘不是"一张 CG"，一次导出可能是几千张：确认框里把组数/张数/角色数
     都写出来，用户才知道自己在点什么（全部角色约 2 万张、几个 GB）。 */
  btn.onclick = async () => {
    if (!window.PACKSTATE || !PACKSTATE.online) {
      await Dlg.alert('导出需要本地服务：请用「启动.bat」（python -m server）打开本页面。',
        { anchor: btn });
      return;
    }
    if (!window.STANDPAGE) return;
    const gs = STANDPAGE.exportGroups();
    const c = STANDPAGE.counts();
    if (!gs.length) {
      await Dlg.alert('当前筛选没有立绘可导出。', { anchor: btn });
      return;
    }
    const who = STANDPAGE.curName();
    const head = who ? `导出角色「${who}」的 ${c.groups} 组 / ${c.imgs} 张立绘？`
      : `导出「${STANDPAGE.scopeName()}」的 ${c.groups} 组 / ${c.imgs} 张立绘？`;
    let tail = who ? '' : '\n（想只导一个角色，先在左侧点一个角色再点导出）';
    /* 导出的就是当前列表：筛选开着时要说清楚，免得用户以为导的是全量 */
    const dlf = ($('#dlFilter') || {}).value || '';
    if (dlf === 'fav') tail += '\n· 现在开着「只看已收藏」，只导出收藏过的服装';
    if (dlf === 'miss') tail += '\n· 现在开着「只看未下载」，只导出本机还没下载的服装';
    const q = (STANDPAGE.query ? STANDPAGE.query() : '').trim();
    if (q) tail += `\n· 搜索框「${q}」也生效，只导出匹配的条目`;
    if (!(await Dlg.confirm(
      `${head}\n\n打包成 zip，存到 userdata/export/。${tail}` +
      `\n\n立绘是整张原图，导得多会很大（可能几个 GB），要跑一阵子。\n\n继续？`,
      { anchor: btn, ok: '开始导出' }))) return;

    btn.disabled = true;
    btn.textContent = '打包中…';
    try {
      const r = await fetch('api/export/stand', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ groups: gs }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
      toast('已开始打包，完成后会提示（大文件需几十秒到几分钟）');
      const jid = (d.job || {}).id;
      const t = setInterval(async () => {
        try {
          const rr = await fetch('api/jobs', { cache: 'no-store' });
          const dd = await rr.json();
          const job = (dd.jobs || []).find(j => j.id === jid);
          if (!job) return;
          if (job.state === 'done') {
            clearInterval(t);
            toast('导出完成：见 userdata/export/');
            btn.disabled = false; btn.textContent = LABEL;
          } else if (job.state === 'failed') {
            clearInterval(t);
            toast('导出失败：' + (job.error || '未知'), true);
            btn.disabled = false; btn.textContent = LABEL;
          } else {
            const pct = (job.percent || 0).toFixed(0);
            btn.textContent = `打包中 ${pct}%`;
          }
        } catch (e) { /* 忽略：下一轮再问 */ }
      }, 800);
    } catch (e) {
      toast(e.message, true);
      btn.disabled = false; btn.textContent = LABEL;
    }
  };
})();
