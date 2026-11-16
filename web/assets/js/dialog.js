/* 全站自绘对话框（替代原生 alert / confirm / prompt）
 * ----------------------------------------------------------------
 * 原生控件是浏览器自带的白框，跟本项目的暗色界面完全割裂（2026-11-15 用户反馈）。
 * 这里提供同一套样式的 Promise 版：
 *
 *   await Dlg.alert('……');
 *   if (!(await Dlg.confirm('确定吗？'))) return;
 *   const name = await Dlg.prompt('名字：', { value: '列表 2' });
 *
 * 为什么是 Promise：DOM 弹窗不可能同步等用户点击，原生 confirm 的同步语义
 * 没法复刻，所以调用点要写成 async 函数 + await（本项目这些点都是事件回调，
 * 加个 async 即可）。
 *
 * 文本一律走 textContent，不拼 innerHTML —— 曲名/列表名是用户可输入的内容。
 */
(function () {
  'use strict';

  var CSS = [
    '#dlgMask{position:fixed;inset:0;z-index:12000;display:flex;align-items:center;',
    '  justify-content:center;padding:20px;background:rgba(6,9,14,.68);',
    '  backdrop-filter:blur(3px);font:14px/1.6 system-ui,"Microsoft YaHei",sans-serif}',
    '#dlgMask.dlg-out{opacity:0;transition:opacity .14s}',
    /* 贴按钮的浮窗：没有蒙层、不居中，点别处/Esc 关掉。
       设置面板、播放列表的＋/✎/🗑 用它 —— 光标和按钮只隔几十像素。 */
    '#dlgPop{position:fixed;z-index:12000;width:min(300px,92vw);',
    '  font:14px/1.6 system-ui,"Microsoft YaHei",sans-serif}',
    '#dlgPop .dlg-box{width:100%;max-height:calc(100vh - 16px);overflow:auto}',
    '#dlgPop.dlg-out{opacity:0;transition:opacity .12s}',
    '.dlg-box{width:min(400px,92vw);box-sizing:border-box;background:#141a24;',
    '  border:1px solid #33445c;border-radius:12px;padding:18px 18px 14px;',
    '  box-shadow:0 18px 50px rgba(0,0,0,.72);color:#c7d0e0;',
    '  animation:dlgIn .16s ease-out}',
    '@keyframes dlgIn{from{opacity:0;transform:translateY(8px) scale(.98)}',
    '  to{opacity:1;transform:none}}',
    '.dlg-box h3{margin:0 0 9px;font-size:15px;color:#e0b566;font-weight:600}',
    '.dlg-msg{white-space:pre-line;line-height:1.75;color:#c7d0e0;',
    '  max-height:52vh;overflow:auto;word-break:break-word}',
    '.dlg-input{display:block;width:100%;box-sizing:border-box;margin:12px 0 2px;',
    '  padding:8px 10px;border:1px solid #3a4a60;border-radius:7px;',
    '  background:#0e1219;color:#e6e9f0;font:inherit;font-size:14px;outline:0}',
    '.dlg-input:focus{border-color:#e0b566;box-shadow:0 0 0 2px rgba(224,181,102,.14)}',
    '.dlg-btns{display:flex;justify-content:flex-end;gap:8px;margin-top:16px}',
    '.dlg-btns button{padding:6px 16px;border-radius:7px;cursor:pointer;font:inherit;',
    '  font-size:14px;border:1px solid #3a4a60;background:#1c2230;color:#c7d0e0}',
    '.dlg-btns button:hover{border-color:#4a3f78;color:#e6e9f0}',
    '.dlg-btns .ok{background:#e0b566;border-color:#e0b566;color:#141822;font-weight:600}',
    '.dlg-btns .ok:hover{background:#eebf6d;color:#141822}',
    '.dlg-btns .ok.danger{background:#c9505a;border-color:#c9505a;color:#fff}',
    '.dlg-btns .ok.danger:hover{background:#d9616b}',
    '.dlg-fields{display:flex;flex-direction:column;gap:9px;margin-top:12px}',
    /* 分组标题要一眼看得出是「组」而不是「字段标签」：
       字段标签是常规字重的灰 Blue，这里用金色 + 700 + 字间距 + 更宽的上间距，
       再靠一条分隔线切开，层级一眼分明（2026-11-15 反馈「跟选项一样」）。 */
    '.dlg-head{margin-top:12px;padding-top:12px;border-top:1px solid #2b3748;',
    '  font-size:14px;font-weight:700;color:#e0b566;letter-spacing:.9px}',
    '.dlg-head:first-child{margin-top:0;padding-top:0;border-top:0}',
    '.dlg-field{display:flex;flex-direction:column;gap:4px;font-size:14px;color:#c7d0e0}',
    '.dlg-field > span{color:#8b93a6}',
    '.dlg-field.check{flex-direction:row;align-items:center;gap:8px;cursor:pointer}',
    '.dlg-field.check > span{color:#c7d0e0}',
    '.dlg-field.check input{accent-color:#e0b566;width:15px;height:15px;flex:none}',
    '.dlg-field select{margin:0;cursor:pointer}',
  ].join('');

  var active = null;

  function fieldHtml(f) {
    /* 分组小标题：把一长串设置按用途分开，避免"一排开关不分主次" */
    if (f.type === 'head') {
      return '<div class="dlg-head">' + f.label + '</div>';
    }
    if (f.type === 'select') {
      var opts = (f.options || []).map(function (o) {
        return '<option value="' + o[0] + '"' +
          (String(o[0]) === String(f.value) ? ' selected' : '') + '>' + o[1] + '</option>';
      }).join('');
      return '<label class="dlg-field"><span>' + f.label + '</span>' +
        '<select class="dlg-input" data-k="' + f.key + '">' + opts + '</select></label>';
    }
    return '<label class="dlg-field check"><input type="checkbox" data-k="' +
      f.key + '"' + (f.value ? ' checked' : '') + '><span>' + f.label + '</span></label>';
  }

  function escFocus() {
    try { document.activeElement && document.activeElement.blur(); } catch (e) { /* 忽略 */ }
  }

  /** 打开一个对话框，返回 Promise。cfg: {title, msg, value, placeholder,
   *  ok, cancel, danger, max, input} */
  function open(cfg) {
    // 一次只留一个：新弹窗顶掉旧的（按"取消"结算，避免悬挂的 Promise）
    if (active) { try { active.close(null); } catch (e) { /* 忽略 */ } }
    if (!document.getElementById('dlgStyle')) {
      var st = document.createElement('style');
      st.id = 'dlgStyle';
      st.textContent = CSS;
      document.head.appendChild(st);
    }
    var prev = document.activeElement;
    /* cfg.anchor = 触发它的按钮：传了就做成贴着按钮的浮窗（无蒙层、不居中），
       不传仍是全屏模态。设置类操作改完还要接着看列表，不该挡视线。 */
    var anchor = (cfg.anchor && cfg.anchor.nodeType === 1) ? cfg.anchor : null;
    return new Promise(function (resolve) {
      var mask = document.createElement('div');
      mask.id = anchor ? 'dlgPop' : 'dlgMask';
      mask.innerHTML =
        '<div class="dlg-box" role="dialog" aria-modal="true">' +
        '<h3></h3><div class="dlg-msg"></div>' +
        (cfg.input ? '<input class="dlg-input" type="text">' : '') +
        (cfg.fields ? '<div class="dlg-fields">' +
          cfg.fields.map(fieldHtml).join('') + '</div>' : '') +
        '<div class="dlg-btns">' +
        (cfg.cancel === null ? '' : '<button class="cancel" type="button"></button>') +
        '<button class="ok" type="button"></button></div></div>';
      var box = mask.firstChild;
      box.querySelector('h3').textContent = cfg.title || '提示';
      box.querySelector('.dlg-msg').textContent = cfg.msg || '';
      // **只找文本框**：select 也带 .dlg-input（同一套样式），以前把它当 input，
      // 于是 input.value='' 把下拉选中项清空、input.select() 直接抛 TypeError，
      // 整个设置面板连保存都走不到（2026-11-15 用户反馈「下拉每次都是空的」）。
      var input = box.querySelector('input.dlg-input');
      if (input) {
        input.value = cfg.value == null ? '' : String(cfg.value);
        input.placeholder = cfg.placeholder || '';
        if (cfg.max) input.maxLength = cfg.max;
      }
      var okBtn = box.querySelector('.ok');
      okBtn.textContent = cfg.ok || '确定';
      if (cfg.danger) okBtn.classList.add('danger');
      var cancelBtn = box.querySelector('.cancel');
      if (cancelBtn) cancelBtn.textContent = cfg.cancel || '取消';

      var done = false;

      /* 浮窗定位：贴在锚点按钮下方（放不下就翻到上方），左右夹在视口内。
         不做全屏蒙层，点别处直接关。 */
      function place() {
        if (!anchor) return;
        var r = anchor.getBoundingClientRect();
        var w = Math.min(300, window.innerWidth - 16);
        mask.style.width = w + 'px';
        var left = Math.min(Math.max(8, r.left), Math.max(8, window.innerWidth - w - 8));
        var h = box.offsetHeight || 0;
        var below = r.bottom + 6;
        var above = r.top - h - 6;
        var top = (below + h + 8 <= window.innerHeight || above < 8) ? below : above;
        mask.style.left = left + 'px';
        mask.style.top = Math.max(8, top) + 'px';
      }

      function onDoc(ev) {
        if (mask.contains(ev.target)) return;
        if (anchor.contains && anchor.contains(ev.target)) return;
        close(cancelVal());
      }

      function close(val) {
        if (done) return;
        done = true;
        active = null;
        mask.classList.add('dlg-out');
        document.removeEventListener('keydown', onKey, true);
        document.removeEventListener('mousedown', onDoc, true);
        window.removeEventListener('resize', place);
        window.removeEventListener('scroll', place, true);
        setTimeout(function () { try { mask.remove(); } catch (e) { /* 忽略 */ } }, 140);
        try { if (prev && prev.focus) prev.focus(); } catch (e) { /* 忽略 */ }
        resolve(val);
      }
      function cancelVal() { return (cfg.input || cfg.fields) ? null : false; }
      function submit() {
        if (cfg.fields) {
          var vals = {}, els = box.querySelectorAll('[data-k]');
          for (var i = 0; i < els.length; i++) {
            var el = els[i];
            vals[el.getAttribute('data-k')] =
              el.type === 'checkbox' ? el.checked : el.value;
          }
          close(vals);
          return;
        }
        close(cfg.input ? input.value : true);
      }
      function onKey(ev) {
        if (ev.key === 'Escape') { ev.preventDefault(); ev.stopPropagation(); close(cancelVal()); }
        else if (ev.key === 'Enter') {
          ev.preventDefault(); ev.stopPropagation(); submit();
        }
      }
      active = { close: close };
      okBtn.onclick = submit;
      if (cancelBtn) cancelBtn.onclick = function () { close(cancelVal()); };
      mask.onclick = function (ev) { if (ev.target === mask) close(cancelVal()); };
      document.addEventListener('keydown', onKey, true);
      (document.body || document.documentElement).appendChild(mask);
      if (anchor) {
        place();                                   // 先插进 DOM 再量高度定位
        document.addEventListener('mousedown', onDoc, true);
        window.addEventListener('resize', place);
        window.addEventListener('scroll', place, true);
      }
      if (input) { input.focus(); input.select(); } else { okBtn.focus(); }
    });
  }

  window.Dlg = {
    alert: function (msg, opts) {
      opts = opts || {};
      return open({ title: opts.title || '提示', msg: msg, ok: opts.ok || '知道了',
        cancel: null, anchor: opts.anchor });
    },
    confirm: function (msg, opts) {
      opts = opts || {};
      return open({
        title: opts.title || '请确认', msg: msg, ok: opts.ok || '确定',
        cancel: opts.cancel || '取消', danger: !!opts.danger, anchor: opts.anchor,
      });
    },
    prompt: function (msg, opts) {
      opts = opts || {};
      return open({
        title: opts.title || '输入', msg: msg, input: true,
        value: opts.value || '', placeholder: opts.placeholder || '',
        max: opts.max || 24, ok: opts.ok || '确定', cancel: opts.cancel || '取消',
        anchor: opts.anchor,
      });
    },
    /** 自绘设置表单：fields = [{key,label,type:'checkbox'|'select',value,options}]
     *  传 cfg.anchor = 触发按钮 → 贴按钮浮窗；不传是居中模态。 */
    form: function (cfg) {
      cfg = cfg || {};
      return open({
        title: cfg.title || '设置', msg: cfg.msg || '', fields: cfg.fields || [],
        ok: cfg.ok || '保存', cancel: cfg.cancel || '取消', anchor: cfg.anchor,
      });
    },
    get busy() { return !!active; },
  };
})();
