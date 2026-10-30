/* 译文导入 / 导出面板
 *
 * 为什么是 txt 块式格式
 * ────────────────────
 * 剧情译文是最需要"人 + AI 协作"的部分：导出原文 → 交给 AI 翻 → 人工抽查 →
 * 导回。JSON 对普通用户不友好（容易漏逗号、编码乱），所以用块式纯文本：
 *
 *   ### 001-p01-m003 【トキサダ】 (2行)
 *   ja: おはよう、今日もいい天気ね。
 *   ja: 　散歩でもしない？
 *   zh: 早上好，今天天气也不错呢。
 *
 * 块头带「编号 + 说话人 + 原文行数」：编号是稳定锚点（AI 改了原文也能落位），
 * 说话人让 AI 定得下语气，行数是游戏文本框的硬约束。以前那套
 * 「原文 ||| 译文」要转义换行、丢了上下文、原文一改就静默失配，已经淘汰。
 *
 * 服务端负责解析/合并（含旧格式嗅探），本文件只负责交互。
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
              <b>### 001-p01-m003 【トキサダ】 (2行)</b>
              <span class="tp-demo">ja: おはよう、今日もいい天気ね。</span><br>
              <span class="tp-demo2">ja: 　散歩でもしない？</span><br>
              <span class="tp-empty">（没有 zh: 行 = 还没翻）</span>
            </div>
            <div class="tp-cmp-f">块头里的编号 / 说话人 / 行数，<b>都别动</b></div>
          </div>
          <div class="tp-cmp-col good">
            <div class="tp-cmp-h">② 填上译文导回来</div>
            <div class="tp-cmp-code">
              <b>### 001-p01-m003 【トキサダ】 (2行)</b>
              <span class="tp-fill">zh: 早上好，今天天气也不错呢。</span><br>
              <span class="tp-fill">zh: 　要不要去散个步？</span>
            </div>
            <div class="tp-cmp-f">原文几行，译文就写几行 <b>zh:</b>（不能多过）</div>
          </div>
        </div>
        <p class="tp-note">
          一句台词 = 一个 <code>###</code> 块，译文写在 <code>zh:</code> 后面，存成 <b>UTF-8</b>。
          不想翻的块留空就行；只写译文不带 <code>ja:</code> 也认。文件末尾的
          <code>### spk:</code> 块是<b>发言人译名</b>（对话框上方的名字），全站共用。
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
        <textarea id="tpText" placeholder="也可以直接把译文粘贴到这里（### 块式，块内写 ja: / zh:）"></textarea>
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
      el.textContent = `译文库 ${t.units || 0} 部 / ${t.messages || 0} 句 · ` +
                       `发言人译名 ${t.speakers || 0} 个`;
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

    // 用当前显示的那句台词做样例，比写死的假名更直观。
    // 只取第一行：样例里是两行 ja，把整句塞进去会折叠成一行看不清结构。
    const demo = (($('#msgText') || {}).textContent || '').trim().split('\n')[0];
    if (demo) {
      p.querySelectorAll('.tp-demo').forEach(el => { el.textContent = 'ja: ' + demo; });
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
        let html =
          `✅ 新增 <b>${d.added}</b> 句，更新 <b>${d.updated}</b> 句，` +
          `跳过 <b>${d.skipped}</b> 句，格式错误 <b>${d.bad}</b> 块；` +
          `本部现有 <b>${d.total}</b> 句译文。<br>`;
        // 这两个是"翻进去了但可能有问题"，不能静默：行数超了会撑破对话框，
        // 原文对不上说明这块可能串行了——都得让人回头看一眼。
        if ((d.lineOver || []).length) {
          html += `<span class="tp-err">译文行数超过原文：${d.lineOver.join('、')}</span>` +
                  `<span class="tp-tip"> 会撑破对话框，建议改短。</span><br>`;
        }
        if ((d.mismatch || []).length) {
          html += `<span class="tp-err">原文对不上：${d.mismatch.join('、')}</span>` +
                  `<span class="tp-tip"> 已按编号/原文落位，建议回头看这几块。</span><br>`;
        }
        if (d.speakers && (d.speakers.added || d.speakers.updated)) {
          html += `发言人译名：新增 <b>${d.speakers.added}</b>，` +
                  `更新 <b>${d.speakers.updated}</b>（共 ${d.speakers.total} 个）<br>`;
        }
        html += `<span class="tp-tip">按 F5 刷新页面即可看到中文。</span>`;
        $('#tpResult').innerHTML = html;
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
