"""解包 UnityFS（LZ4/LZ4HC），直接判定 AB 包内的资源类型。

不再依赖 bundle 命名的推断，用包内特征做硬验证：
- FSB5 魔数        -> FMOD 音频数据（AudioClip 的源数据）
- 日文 UTF-8 序列  -> 文本资源（TextAsset 脚本/表格）
- Texture2D/图片头 -> 贴图
"""
import glob
import os
import struct

from lz4 import block as lz4block


def cstr(buf, pos):
    e = buf.index(b"\0", pos)
    return buf[pos:e].decode("utf-8", "replace"), e + 1


def parse_unityfs(data):
    if data[:7] != b"UnityFS":
        raise ValueError("not UnityFS")
    pos = 8
    version = struct.unpack(">I", data[pos:pos + 4])[0]
    pos += 4
    uver, pos = cstr(data, pos)
    urev, pos = cstr(data, pos)
    size, ci, ui, flags = struct.unpack(">qIII", data[pos:pos + 20])
    pos += 20
    comp = flags & 0x3F

    # flags 位含义：0x40=blocks与目录合并, 0x80=blocksInfo在文件末尾, 0x200=需16字节对齐
    if flags & 0x200:
        pos = (pos + 15) & ~15
    if flags & 0x80:
        bi_pos = len(data) - ci
    else:
        bi_pos = pos
    data_pos = bi_pos + ci
    if flags & 0x200:
        data_pos = (data_pos + 15) & ~15

    bi_raw = data[bi_pos:bi_pos + ci]
    if comp in (2, 3):
        bi = lz4block.decompress(bi_raw, uncompressed_size=ui)
    elif comp == 0:
        bi = bi_raw
    else:
        raise NotImplementedError(f"compression {comp}")
    pos = data_pos

    p = 16
    bcount = struct.unpack(">i", bi[p:p + 4])[0]
    p += 4
    blocks = []
    for _ in range(bcount):
        us, cs, bf = struct.unpack(">IIH", bi[p:p + 10])
        p += 10
        blocks.append((us, cs, bf))
    ncount = struct.unpack(">i", bi[p:p + 4])[0]
    p += 4
    nodes = []
    for _ in range(ncount):
        off, sz, nf = struct.unpack(">qqI", bi[p:p + 20])
        p += 20
        path, p = cstr(bi, p)
        nodes.append((off, sz, nf, path))

    out = bytearray()
    for us, cs, bf in blocks:
        chunk = data[pos:pos + cs]
        pos += cs
        c = bf & 0x3F
        if c in (2, 3):
            out += lz4block.decompress(chunk, uncompressed_size=us)
        elif c == 0:
            out += chunk
        else:
            raise NotImplementedError(f"block compression {c}")

    return bytes(out), nodes, uver, urev, version, comp


def jp_count(buf):
    """统计 UTF-8 编码的日文假名/汉字序列数。"""
    n = 0
    for i in range(len(buf) - 2):
        b0, b1 = buf[i], buf[i + 1]
        if b0 == 0xE3 and 0x80 <= b1 <= 0x83:      # 平假名/片假名/记號
            n += 1
        elif 0xE4 <= b0 <= 0xE9 and 0x80 <= b1 <= 0xBF:  # 汉字
            n += 1
    return n


CLASS_NAMES = ["AudioClip", "TextAsset", "Texture2D", "Sprite", "Font",
               "MonoBehaviour", "AssetBundle", "Mesh", "Material", "Shader",
               "AnimationClip", "GameObject", "Transform", "Texture2DArray"]


def classify(buf):
    """通过 Unity 类名 + 数据魔数判定包内资源类型。"""
    classes = [c for c in CLASS_NAMES if c.encode() in buf]
    magic = []
    if b"FSB5" in buf:
        magic.append(f"FSB5x{buf.count(b'FSB5')}")
    for m, name in ((b"OggS", "OggS"), (b"RIFF", "RIFF"),
                    (b"\x89PNG", "PNG"), (b"KTX", "KTX")):
        if m in buf:
            magic.append(name)
    jp = jp_count(buf)
    return classes, magic, jp


def main():
    print(f"{'样本':<52}{'解压后':>9}  {'包内类名':<34}{'魔数/文本'}")
    print("-" * 132)
    for path in sorted(glob.glob("_sample/*")):
        name = os.path.basename(path)
        try:
            raw = open(path, "rb").read()
            payload, nodes, uver, urev, ver, comp = parse_unityfs(raw)
            classes, magic, jp = classify(payload)
            extra = list(magic)
            if jp > 50:
                extra.append(f"日文x{jp}")
            print(f"{name:<52}{len(payload)/1024:>7.0f}KB  "
                  f"{','.join(classes) if classes else '(无)':<34}"
                  f"{' '.join(extra)}")
        except Exception as e:
            print(f"{name:<52}  解析失败: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
