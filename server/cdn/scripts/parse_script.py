"""解析剧本 JSON 结构的公共模块 + 命令行示例。

extract_jsons() 供其它脚本 import，不再在导入时执行主逻辑。
"""
import json
import re
import sys


def extract_jsons(payload):
    """从 AB 解压数据中提取所有 {"FileName": ...} 的 JSON 对象。"""
    text = payload.decode("utf-8", "ignore")
    out = []
    dec = json.JSONDecoder()
    for m in re.finditer(r'\{"FileName"\s*:', text):
        try:
            obj, _ = dec.raw_decode(text, m.start())
            out.append(obj)
        except Exception:
            continue
    return out


def main():
    from unityfs_probe import parse_unityfs
    path = sys.argv[1] if len(sys.argv) > 1 else \
        "char_story/files/bundle_scenarioscript_char_57"
    payload, *_ = parse_unityfs(open(path, "rb").read())
    js = extract_jsons(payload)
    print(f"{path}: 提取到 {len(js)} 个 JSON")
    for o in js:
        print(f"  FileName={o.get('FileName')}  键={list(o)}")
    if js:
        one = js[0]
        print("\n=== 结构示例（数组只显示前 2 项） ===")
        print(json.dumps({k: (v if not isinstance(v, list)
                              else f"[{len(v)} 项] {v[:2]}")
                          for k, v in one.items()},
                         ensure_ascii=False, indent=2)[:1200])
        print(f"\n=== Methods 前 6 项 ===")
        for item in one.get("Methods", [])[:6]:
            print("  " + json.dumps(item, ensure_ascii=False)[:300])


if __name__ == "__main__":
    main()
