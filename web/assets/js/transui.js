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

  const esc = s => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
  const human = n => {
    n = +n || 0;
    const u = ['B', 'KB', 'MB', 'GB'];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n.toFixed(0) : n.toFixed(i > 1 ? 2 : 1)) + ' ' + u[i];
  };

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
        <b>翻译 · <span id="tpUnit">未选择剧情</span></b>
        <button id="tpClose" title="关闭 Esc">✕</button>
      </div>
      <div class="tp-body">
        <!-- 教程正文**不在这里**：点按钮打开浮窗，用的是与资源管理页
             完全同一份（assets/js/trans_tut.js）。以前这边另写一套说明，
             改一次文案要改两处，必然走样。 -->
        <div class="tp-row">
          <button id="tpTut" type="button">✍ 自己动手翻 · 看 5 步教程</button>
          <span class="tp-hint">导出原文 → 在 zh: 后面填中文 → 导回来</span>
        </div>
        <div class="tp-row">
          <button id="tpExport" class="primary">⬇ 导出原文（.txt）</button>
          <span id="tpStat" class="tp-stat"></span>
        </div>
        <!-- 导出位置与资源管理页的导出**同一个目录**，开关也共用同一个设置
             （localStorage 的 escah_open_exp，两边改哪边都同步） -->
        <div class="tp-row">
          <label class="tp-chk"><input type="checkbox" id="tpAutoOpen"> 导出完成后自动打开文件夹</label>
          <button id="tpOpenExp" type="button" class="tp-mini">打开导出目录</button>
        </div>
        <div id="tpExpLast" class="tp-last">导出后，这里会显示文件名和保存位置。</div>
        <div class="tp-row">
          <!-- 选文件和「打开导出目录」这类次要按钮同一套外观：
               原来是虚线全宽大框，和旁边的按钮完全两个风格，很割裂 -->
          <label class="tp-file">
            <input type="file" id="tpFile" accept=".txt,text/plain">
            <span id="tpFileName">📄 选择译好的 txt…</span>
          </label>
          <span class="tp-hint">选中后会自动填进下面的框，看一眼再点「导入并应用」</span>
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
      el.textContent = `翻译库 ${t.units || 0} 部 / ${t.messages || 0} 句 · ` +
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

    // 教程浮窗：正文在 assets/js/trans_tut.js（与资源管理页共用同一份）
    const tut = p.querySelector('#tpTut');
    if (tut && window.TRANS_TUT) {
      tut.onclick = () => TRANS_TUT.open();
    } else if (tut) {
      tut.style.display = 'none';          // 脚本没加载就别留个点不动的按钮
    }

    /* 导出原文：直接写进**导出目录**（userdata/export，与资源管理页的导出同一个地方），
       不再让浏览器下载 —— 下载会落到系统「下载」文件夹，用户回头根本找不到。 */
    const OPEN_EXP_KEY = 'escah_open_exp';        // 与资源管理页共用同一个设置
    const readAuto = () => {
      try { return localStorage.getItem(OPEN_EXP_KEY) === '1'; } catch (e) { return false; }
    };
    const autoBox = p.querySelector('#tpAutoOpen');
    if (autoBox) {
      autoBox.checked = readAuto();
      autoBox.onchange = () => {
        try {
          localStorage.setItem(OPEN_EXP_KEY, autoBox.checked ? '1' : '0');
        } catch (e) { /* 无痕模式读不到设置，忽略 */ }
        toast(autoBox.checked ? '导出完成后会自动打开文件夹' : '已关闭自动打开文件夹');
      };
    }
    const openExpDir = async () => {
      try {
        const r = await fetch('api/open', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ which: 'export' }),
        });
        const d = await r.json().catch(() => ({}));
        toast('导出目录：' + (d.path || ''));
      } catch (e) {
        alert('打不开导出目录（本地服务未运行？）');
      }
    };
    const openExpBtn = p.querySelector('#tpOpenExp');
    if (openExpBtn) openExpBtn.onclick = openExpDir;

    $('#tpExport').onclick = async () => {
      if (!unit) { toast('先选一部剧情'); return; }
      const btn = $('#tpExport');
      btn.disabled = true;
      btn.textContent = '正在导出…';
      try {
        const r = await fetch('api/export/trans/save', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ unit }),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
        // 结论留在面板上：提示几秒就没了，回头要找"导到哪去了"得有个地方看
        const box = p.querySelector('#tpExpLast');
        if (box) {
          box.innerHTML = '上次导出：<b>' + esc(d.name) + '</b>（' + human(d.bytes) + '）'
            + '<br>保存在 <b>' + esc(d.dir || '') + '</b>';
        }
        $('#tpText').value = '';
        toast('已导出到导出目录，可交给 AI 翻译');
        if (readAuto()) openExpDir();
      } catch (e) {
        alert('导出失败：' + e.message);
      } finally {
        btn.disabled = false;
        btn.textContent = '⬇ 导出原文（.txt）';
      }
    };

    $('#tpFile').onchange = async ev => {
      const f = ev.target.files && ev.target.files[0];
      if (!f) return;
      // 带上图标：选了文件后"这是一份文件"的观感还在，也还看得出能再点一下换文件
      $('#tpFileName').textContent = '📄 ' + f.name;
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
    // 教程浮窗盖在面板上面时，Esc 只关教程（别连面板一起关掉）
    if (window.TRANS_TUT && TRANS_TUT.isOpen()) return;
    const p = $('#transPanel');
    if (p && p.classList.contains('on')) { p.classList.remove('on'); e.stopPropagation(); }
  }, true);
})();
