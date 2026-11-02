/* 「自己动手翻」教程 —— **全文案只有这一份**
 *
 * 为什么要单独抽出来
 * ────────────────
 * 这个教程有两个入口：资源管理页（⑤ 翻译 → 标题旁的按钮）和播放器控制条上的
 * 「翻译」面板。以前两边各写一份说明，改一次文案就得改两处，必然走样；
 * 现在两边都只调这个模块，正文只在本文件的 HTML 常量里。
 *
 * 用法
 * ────
 *   <link rel="stylesheet" href="assets/css/trans_tut.css?v=xxxx">
 *   <script src="assets/js/trans_tut.js?v=xxxx"></script>
 *   TRANS_TUT.bind('#btnTutTrans');    // 点这个按钮就打开
 *   TRANS_TUT.open(); TRANS_TUT.close(); TRANS_TUT.isOpen();
 *
 * 浮窗的 DOM 是**第一次点开时才建**并挂在 body 末尾，页面 HTML 里不用拷结构。
 * z-index 10300：要压过右下角的「版本号 / 免责声明」（#dlBar 10100）
 * 和播放器的右侧译文面板（9500）。
 */
window.TRANS_TUT = (() => {
  const TITLE = '自己动手翻 · 照这 5 步做';

  /* 正文。为什么用整段 HTML 而不是拼 JS 对象：改文案时一眼能看出最终排版，
   * 少一层拼装逻辑就少一处能写错的地方。改这里两个页面同时生效。 */
  const TUT_HTML = `
  <p class="tut-intro">
    最省事的是<b>整段丢给 AI 翻</b>（文件开头就写好了给 AI 的要求）。
    想自己一句句填，照着下面做就行，不用懂任何格式。
  </p>
  <div class="tut">
    <div class="st"><b>导出原文。</b>哪个顺手用哪个，拿到的可能是<b>一个 zip，也可能就是一个 txt</b>：<br>
      · 在「⑤ 翻译 → 批量导出」勾一部剧情 →「导出所选剧情」，得到 <code>译文_日期.zip</code>；<br>
      · 或者播放器里打开那部剧情 → 控制条「翻译」→ 导出原文，<b>直接得到单个 txt</b>，
      只翻一部时走这个，不用解压。</div>
    <div class="st"><b>用记事本打开那个 txt。</b>
      拿到 <b>zip</b>：右键 →「全部解压缩」，里面是 <code>char_415_トキサダ.txt</code> 这种文件；
      拿到 <b>txt</b>：跳过解压，直接看。
      然后右键这个 txt →「打开方式」→「记事本」。开头一大段是说明，<b>不用管</b>，
      往下拉就是一句一句的台词。</div>
    <div class="st"><b>只在 <code>zh:</code> 后面写中文。</b>
      每一句长下面这样 —— <code>###</code> 开头那行是这句的编号，<b>一个字都别改</b>：
      <pre class="tut-code">### 001-p01-m003 【トキサダ】 (2行)
ja: 「それで、お前たちは警備中に
ja: 幻魔と出くわしたってのか」</pre>
      要做的是在下面<b>添上 zh: 行</b>（一句一行，行数与括号内 <code>(2行)</code> 的行数保持一致）：
      <pre class="tut-code">### 001-p01-m003 【トキサダ】 (2行)
ja: 「それで、お前たちは警備中に
ja: 幻魔と出くわしたってのか」
zh: 「所以说，你们是在执行警备时
zh: 撞上幻魔的？」</pre>
      <b>ja: 那几行一个字都别动</b>（改了就认不出是哪一句）。三条规矩：
      <b>块头（### 那行）不许改、不许删、不许把两句合成一块</b>；
      译文<b>必须写在 zh: 后面</b>；某句不想翻就<b>留空、或者干脆不写 zh 行</b>，不会报错。</div>
    <div class="st"><b>保存文件。</b>记事本按 <b>Ctrl+S</b> 直接存就行
      （编码保持 UTF-8，别「另存为」改成 ANSI）。</div>
    <div class="st"><b>导回来。</b>回「⑤ 翻译 → 导入译文」选这个 txt
      （选整个 zip 也行，那边不用先解压）→ 点「导入」；
      在播放器那个面板里就是选文件后点「导入并应用」。
      最后按 <b>F5</b> 刷新，那部剧情就有中文了。
      <span class="tut-sub">导完句数会变多 —— 资源管理页「现在有多少中文」里能看到。</span></div>
  </div>
  <p class="tut-outro">
    交给 AI 时把<b>整个 txt</b> 贴给它就行（拿到的是 zip，解压后的 txt 也一样），
    开头那段「翻译要求」它自己会看；翻好存成 txt 再导回来。
    <b>不放心可以先只翻一两句导进来试</b>，看到效果再全翻。
  </p>`;

  let mask = null;
  let prevOverflow = '';

  function build() {
    mask = document.createElement('div');
    mask.className = 'tutMask';
    mask.hidden = true;
    mask.innerHTML = '<div class="tutBox" role="dialog" aria-modal="true"'
      + ' aria-label="自己动手翻的 5 个步骤">'
      + '<div class="tutHead"><b>' + TITLE + '</b>'
      + '<button type="button" class="tutClose" title="关闭（Esc）">✕ 关闭</button></div>'
      + '<div class="tutBody">' + TUT_HTML + '</div></div>';
    document.body.appendChild(mask);
    // 点遮罩空白处、或点「✕ 关闭」都关
    mask.addEventListener('click', ev => {
      if (ev.target === mask || ev.target.closest('.tutClose')) close();
    });
    /* Esc：**必须用捕获 + stopPropagation**。播放器的译文面板也监听 Esc
     * （capture），不拦住的话会连面板一起关掉。 */
    document.addEventListener('keydown', ev => {
      if (!mask || mask.hidden) return;
      if (ev.key === 'Escape') { ev.stopPropagation(); close(); }
    }, true);
  }

  function open() {
    if (!mask) build();
    mask.hidden = false;
    // 浮窗开着时别让背后的页面跟着滚
    prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
  }

  function close() {
    if (!mask) return;
    mask.hidden = true;
    document.body.style.overflow = prevOverflow || '';
  }

  const isOpen = () => !!mask && !mask.hidden;

  /** 把某个按钮绑成"点开教程"；选择器找不到就算了（不是每个页面都有入口） */
  function bind(sel) {
    const el = typeof sel === 'string' ? document.querySelector(sel) : sel;
    if (el) el.addEventListener('click', open);
    return !!el;
  }

  return { open, close, isOpen, bind };
})();
