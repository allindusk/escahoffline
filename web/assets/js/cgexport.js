/* CG 页的「导出」按钮
 *
 * 打包在服务端做（浏览器没有 zip 能力，也不想为这个引第三方库）。
 * 这里只负责：算清楚"要导哪些角色" → POST /api/export/cg → 显示进度。
 * 没有本地服务时直接说明不可用，不假装成功。
 */
(() => {
  const $ = s => document.querySelector(s);

  function toast(msg, bad) {
    let el = $('#cgToast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'cgToast';
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

  const btn = $('#btnExportCg');
  if (!btn) return;

  btn.onclick = async () => {
    if (!window.PACKSTATE || !PACKSTATE.online) {
      alert('导出需要本地服务：请用「启动.bat」（python -m server）打开本页面。');
      return;
    }
    const cur = window.CGPAGE ? CGPAGE.cur() : null;
    let body;
    if (cur) {
      if (!confirm(`导出角色「${CGPAGE.nameOf(cur)}」的全部 CG？`)) return;
      body = { chars: [cur] };
    } else {
      const c = window.CGPAGE ? CGPAGE.counts() : { chars: 0, total: 0 };
      if (!confirm(`当前是「全部角色」，将打包 ${c.chars} 个角色、共 ${c.total} 张 CG。\n\n` +
        `（想只导一个角色，先在左侧点一个角色再点导出）\n\n继续？`)) return;
      body = { whole: true };
    }
    btn.disabled = true;
    btn.textContent = '打包中…';
    try {
      const r = await fetch('api/export/cg', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
      toast('已开始打包，完成后会提示（大文件需几十秒）');
      // 轮询 job 状态（比 SSE 简单，这里只有一件小事）
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
            btn.disabled = false; btn.textContent = '⬇ 导出 CG';
          } else if (job.state === 'failed') {
            clearInterval(t);
            toast('导出失败：' + (job.error || '未知'), true);
            btn.disabled = false; btn.textContent = '⬇ 导出 CG';
          } else {
            btn.textContent = `打包中 ${(job.percent || 0).toFixed(0)}%`;
          }
        } catch (e) { /* 忽略 */ }
      }, 800);
    } catch (e) {
      toast(e.message, true);
      btn.disabled = false; btn.textContent = '⬇ 导出 CG';
    }
  };
})();
