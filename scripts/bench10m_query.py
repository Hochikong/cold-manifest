#!/usr/bin/env python3
"""10M 查询压测：TestClient 进程内打点（不碰 8765 端口）。

覆盖：entries 首页/深页 keyset、tree、du、stats（冷/热）、search prefix、
fulltext 命中/不命中。输出 JSON 到 <data_root>/query_timings.json。

用法：python scripts/bench10m_query.py <data_root>
"""
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cold_manifest.server import create_app  # noqa: E402

DATA_ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/opencode/bench10m")
SNAP = "BENCH10M_P0/20260928T000001Z"

app = create_app(data_root=str(DATA_ROOT))


def timed(client, url: str, repeat: int = 3) -> dict:
    """返回 {ms 列表统计, http 状态, 采样 item 数}。首次调用即冷态。"""
    times, status, n_items = [], None, None
    for i in range(repeat):
        t = time.perf_counter()
        r = client.get(url)
        times.append((time.perf_counter() - t) * 1000)
        status = r.status_code
        if status == 200:
            body = r.json()
            n_items = len(body.get("items") or body.get("dirs") or [])
        else:
            n_items = r.text[:120]
    return {"url": url, "status": status, "items": n_items,
            "ms": [round(x, 1) for x in times],
            "ms_min": round(min(times), 1), "ms_median": round(statistics.median(times), 1)}


def main() -> None:
    from fastapi.testclient import TestClient
    results: "list[dict]" = []
    with TestClient(app) as client:
        # 找一个子项最多的目录（深页压测用），及任意一个深层目录
        import sqlite3
        db = DATA_ROOT / "BENCH10M_P0" / "20260928T000001Z" / "snapshot.db"
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro&immutable=1", uri=True)
        big_dir = conn.execute(
            "SELECT parent_id, COUNT(*) n FROM entries GROUP BY parent_id"
            " ORDER BY n DESC LIMIT 1").fetchone()
        deep_dir = conn.execute(
            "SELECT parent_id FROM entries WHERE depth=12 AND type='file' LIMIT 1").fetchone()
        conn.close()
        pid_big = big_dir[0] if big_dir else 2
        pid_deep = deep_dir[0] if deep_dir else 2

        cases = [
            ("entries 首页（root）", f"/api/snapshots/{SNAP}/entries?parent_id=0&limit=100"),
            ("entries 大目录首页（%d 子项）" % big_dir[1],
             f"/api/snapshots/{SNAP}/entries?parent_id={pid_big}&limit=100"),
            ("entries 大目录第 100 页（keyset 深翻页）", None),   # 下面滚动游标生成
            ("entries 深层目录（depth 12）",
             f"/api/snapshots/{SNAP}/entries?parent_id={pid_deep}&limit=100"),
            ("entries 按 size 排序（大目录）",
             f"/api/snapshots/{SNAP}/entries?parent_id={pid_big}&sort=size&limit=100"),
            ("tree root", f"/api/snapshots/{SNAP}/tree?parent_id=0"),
            ("tree 大目录", f"/api/snapshots/{SNAP}/tree?parent_id={pid_big}"),
            ("du root", f"/api/snapshots/{SNAP}/du?parent_id=0"),
            ("du 大目录", f"/api/snapshots/{SNAP}/du?parent_id={pid_big}"),
            ("stats 冷（进程首查）", None),   # 单独处理
            ("stats 热", f"/api/snapshots/{SNAP}/stats"),
            ("search prefix 'f12345'", f"/api/snapshots/{SNAP}/search?q=f12345&mode=prefix&limit=50"),
            ("search prefix 'd1234'", f"/api/snapshots/{SNAP}/search?q=d1234&mode=prefix&limit=50"),
            ("fulltext 命中 'f999999.bin'", f"/api/snapshots/{SNAP}/search?q=f999999&mode=fulltext&limit=50"),
            ("fulltext 不命中 'zzzzqqqq'", f"/api/snapshots/{SNAP}/search?q=zzzzqqqq&mode=fulltext&limit=50"),
            ("fulltext 高频子串 'd1234/'", f"/api/snapshots/{SNAP}/search?q=d1234/&mode=fulltext&limit=50"),
        ]

        # stats 冷：开一个新 client（新 LRU 连接）只打一枪
        with TestClient(app) as cold:
            results.append(timed(cold, f"/api/snapshots/{SNAP}/stats", repeat=1))
        results[-1]["url"] = "stats 冷（进程首查）"

        # 深翻页：从大目录首页开始滚 100 页游标
        r = client.get(f"/api/snapshots/{SNAP}/entries?parent_id={pid_big}&limit=100")
        cursor, pages = r.json().get("next_cursor"), 1
        while cursor and pages < 100:
            r = client.get(f"/api/snapshots/{SNAP}/entries?parent_id={pid_big}"
                           f"&limit=100&cursor={cursor}")
            cursor, pages = r.json().get("next_cursor"), pages + 1
        results.append({"url": f"entries 大目录第 {pages} 页（keyset 深翻页,滚动不计时）",
                        "status": r.status_code, "items": len(r.json().get("items", [])),
                        "ms": [], "ms_min": None, "ms_median": None})

        for name, url in cases:
            if url is None:
                continue
            row = timed(client, url)
            results.append({"url": name, **{k: v for k, v in row.items() if k != "url"}})
            print(results[-1])

    out = DATA_ROOT / "query_timings.json"
    out.write_text(json.dumps(results, indent=1, ensure_ascii=False))
    print("已写", out)


if __name__ == "__main__":
    main()
