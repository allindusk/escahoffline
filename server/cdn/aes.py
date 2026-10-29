"""零依赖 AES-256-CBC 解密（仅解密，不实现加密）。

为什么自己实现
--------------
CDN 清单是用 Base64 文本 + AES-256-CBC 下发的。项目机上没有
``pycryptodome`` / ``cryptography``，旧方案是用 PowerShell 调 .NET
（``_scripts/decrypt.ps1``）绕开依赖。为了把 update 链路收敛成纯 Python，
这里内置一个只用标准库的实现。

S-box 不是硬编码的 256 个十六进制常量（容易抄错），而是运行时用 GF(2^8)
运算推导出来，并在 :func:`selftest` 里用 FIPS-197 官方测试向量校验。

**不要用这个类去加密业务数据**——它是为解这一份清单写的教学级实现，
没有做侧信道防护，也没有常量时间比较。
"""

from __future__ import annotations

# ---------------------------------------------------------------- GF(2^8)


def _xtime(a: int) -> int:
    """乘 2（模 AES 多项式 0x11B）。"""
    a <<= 1
    if a & 0x100:
        a = (a ^ 0x1B) & 0xFF
    return a & 0xFF


def _build_tables():
    """推导 AES S-box / 逆 S-box。

    取 GF(2^8) 的生成元 3，先造指数表/对数表，再对每个非零元素求逆，
    最后套仿射变换得到 S-box。
    """
    exp = [0] * 256
    log = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x = _xtime(x) ^ x          # ×3（生成元）

    def inv(a: int) -> int:
        if a == 0:
            return 0
        # 注意取模：a==1 时 log==0，若直接用 exp[255] 会取到未填充的槽位
        return exp[(255 - log[a]) % 255]

    sbox = [0] * 256
    inv_sbox = [0] * 256
    for i in range(256):
        b = inv(i)
        s = b
        for r in range(1, 5):      # 循环左移 1/2/3/4 位后连异或
            s ^= ((b << r) | (b >> (8 - r))) & 0xFF
        s ^= 0x63
        sbox[i] = s
        inv_sbox[s] = i
    return sbox, inv_sbox, exp, log


_SBOX, _INV_SBOX, _EXP, _LOG = _build_tables()

_NK_BY_KEYLEN = {16: 4, 24: 6, 32: 8}
_NR_BY_NK = {4: 10, 6: 12, 8: 14}

_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36,
         0x6C, 0xD8, 0xAB, 0x4D]


def _gmul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[(_LOG[a] + _LOG[b]) % 255]


class AES:
    """只用标准库的 AES 解密器（ECB 模式，CBC 由 :func:`cbc_decrypt` 负责）。"""

    def __init__(self, key: bytes) -> None:
        if len(key) not in _NK_BY_KEYLEN:
            raise ValueError(f"AES 密钥长度必须是 16/24/32 字节，收到 {len(key)}")
        self.nk = _NK_BY_KEYLEN[len(key)]
        self.nr = _NR_BY_NK[self.nk]
        self.round_keys = self._expand(key)

    # ------------------------------------------------------------ 密钥扩展
    def _expand(self, key: bytes):
        w = [list(key[4 * i:4 * i + 4]) for i in range(self.nk)]
        for i in range(self.nk, 4 * (self.nr + 1)):
            t = list(w[i - 1])
            if i % self.nk == 0:
                t = t[1:] + t[:1]                       # RotWord
                t = [_SBOX[b] for b in t]               # SubWord
                t[0] ^= _RCON[i // self.nk - 1]         # Rcon
            elif self.nk > 6 and i % self.nk == 4:
                t = [_SBOX[b] for b in t]
            w.append([w[i - self.nk][j] ^ t[j] for j in range(4)])
        return w

    # ------------------------------------------------------------ 解密主体
    def decrypt_block(self, block: bytes) -> bytes:
        if len(block) != 16:
            raise ValueError("块长度必须为 16 字节")
        s = [[0] * 4 for _ in range(4)]
        for c in range(4):                               # 字节 -> 状态矩阵（列优先）
            for r in range(4):
                s[r][c] = block[c * 4 + r]

        def add_round_key(rnd: int) -> None:
            for c in range(4):
                for r in range(4):
                    s[r][c] ^= self.round_keys[rnd * 4 + c][r]

        add_round_key(self.nr)
        for rnd in range(self.nr - 1, 0, -1):
            # InvShiftRows
            for r in range(1, 4):
                s[r] = s[r][-r:] + s[r][:-r]
            # InvSubBytes
            for c in range(4):
                for r in range(4):
                    s[r][c] = _INV_SBOX[s[r][c]]
            add_round_key(rnd)
            # InvMixColumns
            for c in range(4):
                a = s[0][c]
                b = s[1][c]
                d = s[2][c]
                e = s[3][c]
                s[0][c] = _gmul(a, 14) ^ _gmul(b, 11) ^ _gmul(d, 13) ^ _gmul(e, 9)
                s[1][c] = _gmul(a, 9) ^ _gmul(b, 14) ^ _gmul(d, 11) ^ _gmul(e, 13)
                s[2][c] = _gmul(a, 13) ^ _gmul(b, 9) ^ _gmul(d, 14) ^ _gmul(e, 11)
                s[3][c] = _gmul(a, 11) ^ _gmul(b, 13) ^ _gmul(d, 9) ^ _gmul(e, 14)
        # 最后一轮
        for r in range(1, 4):
            s[r] = s[r][-r:] + s[r][:-r]
        for c in range(4):
            for r in range(4):
                s[r][c] = _INV_SBOX[s[r][c]]
        add_round_key(0)

        return bytes(s[r][c] for c in range(4) for r in range(4))


# ---------------------------------------------------------------- CBC


def pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        return data
    n = data[-1]
    if 1 <= n <= 16 and data.endswith(bytes([n]) * n):
        return data[:-n]
    return data                      # 可能是 unknown padding，原样返回让上层判断


def cbc_decrypt(data: bytes, key: bytes, iv: bytes, unpad: bool = True) -> bytes:
    """AES-256-CBC 解密。``data`` 长度必须是 16 的倍数。

    **优先用系统/三方实现**：资源清单约 5 MB，纯 Python 版本要 30–60 秒，
    而 ``cryptography`` 只要几十毫秒。两者结果完全一致（有 FIPS-197 自检兜底），
    所以这里做"有就用快的、没有就退回能跑的"。
    """
    if len(key) != 32:
        raise ValueError(f"需要 32 字节密钥，收到 {len(key)}")
    if len(iv) != 16:
        raise ValueError(f"需要 16 字节 IV，收到 {len(iv)}")
    if len(data) % 16:
        raise ValueError(f"密文长度 {len(data)} 不是 16 的倍数")

    fast = _decrypt_cryptography(data, key, iv)
    if fast is not None:
        return pkcs7_unpad(fast) if unpad else fast
    fast = _decrypt_pycryptodome(data, key, iv)
    if fast is not None:
        return pkcs7_unpad(fast) if unpad else fast

    aes = AES(key)
    out = bytearray()
    prev = iv
    for i in range(0, len(data), 16):
        cur = data[i:i + 16]
        dec = aes.decrypt_block(cur)
        out += bytes(dec[j] ^ prev[j] for j in range(16))
        prev = cur
    return pkcs7_unpad(bytes(out)) if unpad else bytes(out)


def _decrypt_cryptography(data: bytes, key: bytes, iv: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers import (Cipher, algorithms,
                                                            modes)
    except ImportError:
        return None
    d = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return d.update(data) + d.finalize()


def _decrypt_pycryptodome(data: bytes, key: bytes, iv: bytes):
    try:
        from Crypto.Cipher import AES as _AES
    except ImportError:
        return None
    return _AES.new(key, _AES.MODE_CBC, iv).decrypt(data)


def backend() -> str:
    """当前用的是哪套 AES 实现（给 doctor / 日志用）。"""
    try:
        import cryptography                                  # noqa: F401
        return "cryptography（快）"
    except ImportError:
        pass
    try:
        import Crypto                                        # noqa: F401
        return "pycryptodome（快）"
    except ImportError:
        pass
    return "内置纯 Python（慢，清单解密约 30–60 秒）"


# ---------------------------------------------------------------- 自检


def selftest() -> bool:
    """用 FIPS-197 C.3 的 AES-256 测试向量校验实现是否正确。"""
    key = bytes(range(32))                                        # 00 01 … 1f
    pt = bytes.fromhex("00112233445566778899aabbccddeeff")
    ct = bytes.fromhex("8ea2b7ca516745bfeafc49904b496089")
    got = AES(key).decrypt_block(ct)
    if got != pt:
        raise AssertionError(f"AES-256 自检失败: {got.hex()} != {pt.hex()}")
    return True


if __name__ == "__main__":
    selftest()
    print("aes selftest OK")
