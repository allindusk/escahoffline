/* 下载源配置的前端解密层
 *
 * 服务端（server/packs.py）也解同一份数据，算法必须完全一致：
 *     明文 JSON(UTF-8) --RC4--> 字节 --base64--> 字符串
 *
 * 为什么前端也要能解
 * ─────────────────
 * 1. 纯静态部署（web/ 丢到任意静态服务器，甚至 file:// 打开）时没有后端，
 *    「下载源」只能靠前端解密 + localStorage 覆盖；
 * 2. 自检方便：F12 里 `PACK_LINKS.urlFor('core')` 立刻看到最终请求地址。
 *
 * 注意：这里必须做**纯字节**运算。用 TextEncoder/TextDecoder 包住 RC4 会把
 * 0x80–0xFF 的字节变成两字节 UTF-8，和 Python 端对不上。
 *
 * 改密钥时三处要一起改：
 *   server/packs.py 的 RC4_KEY
 *   tools/encrypt_links.py 的 --key
 *   本文件的 RC4_KEY
 */
window.PACK_LINKS = (() => {
  const RC4_KEY = 'escah-offline-v1';
  const LS_KEY = 'escah_links';

  function rc4Bytes(bytes, keyStr) {
    const k = new TextEncoder().encode(keyStr);
    const s = new Uint8Array(256);
    for (let i = 0; i < 256; i++) s[i] = i;
    let j = 0;
    for (let i = 0; i < 256; i++) {
      j = (j + s[i] + k[i % k.length]) & 0xff;
      const t = s[i]; s[i] = s[j]; s[j] = t;
    }
    const out = new Uint8Array(bytes.length);
    let i = 0; j = 0;
    for (let n = 0; n < bytes.length; n++) {
      i = (i + 1) & 0xff;
      j = (j + s[i]) & 0xff;
      const t = s[i]; s[i] = s[j]; s[j] = t;
      out[n] = bytes[n] ^ s[(s[i] + s[j]) & 0xff];
    }
    return out;
  }

  function b64ToBytes(b64) {
    const raw = atob(String(b64).replace(/\s+/g, ''));
    const out = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }

  function bytesToB64(bytes) {
    let bin = '';
    for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin);
  }

  function decrypt(b64) {
    const plain = rc4Bytes(b64ToBytes(b64), RC4_KEY);
    return JSON.parse(new TextDecoder('utf-8').decode(plain));
  }

  function encrypt(obj) {
    const raw = new TextEncoder().encode(JSON.stringify(obj));
    return bytesToB64(rc4Bytes(raw, RC4_KEY));
  }

  let builtin = {};
  try {
    if (window.__PACK_LINKS) builtin = decrypt(window.__PACK_LINKS);
  } catch (e) {
    console.warn('下载源配置解密失败（密钥不一致？）', e);
  }

  function custom() {
    try { return JSON.parse(localStorage.getItem(LS_KEY) || 'null') || null; }
    catch (e) { return null; }
  }

  function cfg() {
    return Object.assign({ base: '', pattern: '{id}.zip', overrides: {} },
      builtin, custom() || {});
  }

  function urlFor(id) {
    const c = cfg();
    if (c.overrides && c.overrides[id]) return c.overrides[id];
    if (!c.base) return '';
    const base = String(c.base).replace(/\/+$/, '');
    return base + '/' + String(c.pattern || '{id}.zip').replace('{id}', id);
  }

  function saveCustom(obj) {
    if (obj && obj.base) localStorage.setItem(LS_KEY, JSON.stringify(obj));
    else localStorage.removeItem(LS_KEY);
  }

  return {
    cfg, urlFor, saveCustom, encrypt, decrypt,
    get builtin() { return builtin; },
    get custom() { return custom(); },
  };
})();
