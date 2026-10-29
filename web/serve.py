"""兼容入口：``python web/serve.py``

真正的服务在项目根的 ``server/`` 包里（静态站点 + 资源管理 API）。
保留本文件是为了让老用户的习惯命令和旧快捷方式继续能用。

    python web/serve.py            # 等价于 python -m server --open
    python web/serve.py -p 8012
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)


def main(argv=None) -> int:
    try:
        from server.__main__ import main as real
    except Exception:                                            # noqa: BLE001
        print("找不到 server/ 包。请从项目根目录运行： python -m server")
        return 1
    args = list(sys.argv[1:] if argv is None else argv)
    if "-p" not in args and "--port" not in args:
        args += ["--open"]
    return real(args)


if __name__ == "__main__":
    sys.exit(main())
