/* 译文导入 / 导出面板
 *
 * 为什么要走 txt
 * ─────────────
 * 剧情译文是最需要"人 + AI 协作"的部分：导出原文 → 交给 AI 翻 → 人工抽查 →
 * 导回。JSON 对普通用户不友好（容易漏逗号、编码乱），所以用「原文 ||| 译文」
 * 的单行文本格式，任何编辑器都能改，任何 AI 都看得懂格式说明（文件头有注释）。
 *
 * 服务端负责合并（保留已有译文、新增/覆盖计数），本文件只负责交互。
 */
(() => {
  const $ = s => document.querySelector(s);

  function toast(msg) {
    if (window.PLAYER_API) return PLAYER_API.toast(msg);
    const el = $('#toast');
    if (el) { el.textContent = msg; el.classList.add('on'); setTimeout(() => el.classList.remove('on'), 2000); }
  }

  function api() {
    return window.PACKSTATE && PACKSTATE.online;
  }

  function panel() {
    let p = $('#transPanel');
    if (p) return p;
    p = document.createElement('div');
    p.id = 'transPanel';
    p.innerHTML = `
      <div class="tp-head">
        <b>译文 · <span id="tpUnit">未选择剧情</span></b>
        <button id="tpClose" title="关闭 Esc">✕</button>
      </div>
      <div class="tp-body">
        <!-- 左右对比：左边是"导出后你会看到的"，右边是"翻好后应该变成的"。
             光写规则很多人看不懂，直接并排给样例最省事。 -->
        <div class="tp-cmp">
          <div class="tp-cmp-col">
            <div class="tp-cmp-h">① 导出后长这样</div>
            <div class="tp-cmp-code">
              <span class="tp-demo">おはよう、今日もいい天気ね。</span><br>
              <span class="tp-bar">|||</span>
              <span class="tp-empty">（空的，等翻译）</span>
            </div>
            <div class="tp-cmp-f">左边是原文，<b>不要动</b></div>
          </div>
          <div class="tp-cmp-col good">
            <div class="tp-cmp-h">② 翻好后导回来</div>
            <div class="tp-cmp-code">
              <span class="tp-demo">おはよう、今日もいい天気ね。</span><br>
              <span class="tp-bar">|||</span>
              <span class="tp-fill">早上好，今天天气也不错呢。</span>
            </div>
            <div class="tp-cmp-f">只填竖线右边，<b>留空 = 跳过这句</b></div>
          </div>
        </div>
        <p class="tp-note">
          分隔符是 <code>两个半角竖线</code> + 两边各一个空格；文件保存为 <b>UTF-8</b>。
          行数、左边原文都可以不用改，只补右边就行。
        </p>
        <div class="tp-row">
          <button id="tpExport" class="primary">⬇ 导出原文（.txt）</button>
          <span id="tpStat" class="tp-stat"></span>
        </div>
        <div class="tp-row">
          <label class="tp-file">
            <input type="file" id="tpFile" accept=".txt,text/plain">
            <span id="tpFileName">选择译好的 txt…</span>
          </label>
        </div>
        <textarea id="tpText" placeholder="也可以直接把译文粘贴到这里（格式：原文 ||| 译文）"></textarea>
        <div class="tp-row">
          <label class="tp-mode"><input type="radio" name="tpmode" value="merge" checked> 合并（推荐）</label>
          <label class="tp-mode"><input type="radio" name="tpmode" value="replace"> 覆盖整部</label>
          <button id="tpImport" class="primary">✅ 导入并应用</button>
        </div>
        <div id="tpResult" class="tp-result"></div>
      </div>`;
    document.body.appendChild(p);
    p.querySelector('#tpClose').onclick = () => p.classList.remove('on');
    return p;
  }

  async function refreshStat(unit) {
    const el = $('#tpStat');
    if (!el) return;
    try {
      const r = await fetch('api/state', { cache: 'no-store' });
      const d = await r.json();
      const t = (d.trans || {});
      el.textContent = `译文库已有 ${t.units || 0} 部 / ${t.messages || 0} 句`;
    } catch (e) { el.textContent = '（本地服务未运行，导入不可用）'; }
  }

  function openPanel() {
    const p = panel();
    p.classList.add('on');
    const unit = window.PLAYER_API ? PLAYER_API.key() : '';
    $('#tpUnit').textContent = unit
      ? (PLAYER_API.title(unit) + '（' + unit + '）')
      : '未选择剧情';
    p.dataset.unit = unit || '';
    refreshStat(unit);

    // 用当前显示的那句台词做样例，比写死的假名更直观
    const demo = (($('#msgText') || {}).textContent || '').trim();
    if (demo) {
      p.querySelectorAll('.tp-demo').forEach(el => { el.textContent = demo; });
    }

    $('#tpExport').onclick = async () => {
      if (!unit) { toast('先选一部剧情'); return; }
      try {
        const r = await fetch('api/export/trans?unit=' + encodeURIComponent(unit));
        if (!r.ok) {
          const e = await r.json().catch(() => ({}));
          alert(e.error || ('导出失败：HTTP ' + r.status));
          return;
        }
        const text = await r.text();
        const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = unit + '.txt';
        a.click();
        setTimeout(() => URL.revokeObjectURL(a.href), 4000);
        $('#tpText').value = '';
        toast('已导出，可交给 AI 翻译');
      } catch (e) {
        alert('本地服务未运行，无法导出。请用「启动.bat」打开后重试。');
      }
    };

    $('#tpFile').onchange = async ev => {
      const f = ev.target.files && ev.target.files[0];
      if (!f) return;
      $('#tpFileName').textContent = f.name;
      $('#tpText').value = await f.text();
    };

    $('#tpImport').onclick = async () => {
      if (!unit) { toast('先选一部剧情'); return; }
      const text = $('#tpText').value;
      if (!text.trim()) { alert('没有内容可导入'); return; }
      const mode = (document.querySelector('input[name=tpmode]:checked') || {}).value || 'merge';
      $('#tpImport').disabled = true;
      $('#tpResult').textContent = '正在合并…';
      try {
        const r = await fetch('api/import/trans', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ unit, text, mode }),
        });
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
        $('#tpResult').innerHTML =
          `✅ 新增 <b>${d.added}</b> 句，更新 <b>${d.updated}</b> 句，` +
          `跳过 <b>${d.skipped}</b> 句，格式错误 <b>${d.bad}</b> 行；` +
          `本部现有 <b>${d.total}</b> 句译文。<br>` +
          `<span class="tp-tip">按 F5 刷新页面即可看到中文。</span>`;
        refreshStat(unit);
      } catch (e) {
        $('#tpResult').innerHTML = `<span class="tp-err">导入失败：${e.message}</span>`;
      } finally {
        $('#tpImport').disabled = false;
      }
    };
  }

  const btn = document.querySelector('#btnTrans');
  if (btn) btn.onclick = openPanel;
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    const p = $('#transPanel');
    if (p && p.classList.contains('on')) { p.classList.remove('on'); e.stopPropagation(); }
  }, true);
})();
