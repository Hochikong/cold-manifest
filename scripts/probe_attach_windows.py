"""在 Windows 上探测 SQLite「ATTACH / file: URI / 只读打开」哪种形式可用。

背景：Windows 上 diff 引擎的 `ATTACH DATABASE 'file:///…?mode=ro&immutable=1'`
报 `unable to open database`，而同一路径用 `sqlite3.connect(uri, uri=True)` 浏览正常。
本脚本把几种候选写法逐一试一遍（connect 与 ATTACH 各来一次），用于定位到底哪种形式可用。

用法（在包根下）：
    .venv-win\\Scripts\\python.exe scripts\\probe_attach_windows.py [snapshot.db 路径]
不传路径时用下面 DEFAULT 里的样本路径（按需改）。
只读探测：不做任何写入。
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys

DEFAULT = pathlib.Path(
    r"D:\Vibe软件\cold-manifest-0.2.0-win\data"
    r"\20260123004775F_P2\20261001T121359Z\snapshot.db"
)


def main() -> int:
    p = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    print(f"目标库：{p}")
    print(f"存在：{p.is_file()}   大小：{p.stat().st_size if p.is_file() else '-'} 字节")
    print(f"as_uri()：{p.as_uri()}")
    print()

    forms: list[tuple[str, str, bool]] = [
        ("A 现状写法 as_uri() + ?mode=ro&immutable=1", p.as_uri() + "?mode=ro&immutable=1", True),
        ("B as_uri() + ?mode=ro（去掉 immutable）", p.as_uri() + "?mode=ro", True),
        ("C as_uri() 不带参数", p.as_uri(), True),
        ("D file: + 正斜杠路径 + ?mode=ro", "file:" + p.as_posix() + "?mode=ro", True),
        ("E 普通路径（非 URI；仅信息参考）", str(p), False),
    ]

    for label, target, is_uri in forms:
        try:
            conn = sqlite3.connect(target, uri=is_uri)
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
            conn_res = "OK"
            conn.close()
        except Exception as exc:  # noqa: BLE001
            conn_res = f"{type(exc).__name__}: {exc}"

        try:
            main = sqlite3.connect(":memory:", uri=True)
            main.execute("ATTACH DATABASE ? AS sna", (target,))
            main.execute("SELECT COUNT(*) FROM sna.entries").fetchone()
            main.close()
            att_res = "OK"
        except Exception as exc:  # noqa: BLE001
            att_res = f"{type(exc).__name__}: {exc}"

        print(label)
        print(f"    connect : {conn_res}")
        print(f"    ATTACH  : {att_res}")
        print()

    print("sqlite 版本：", sqlite3.sqlite_version, "| Python：", sys.version.split()[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
