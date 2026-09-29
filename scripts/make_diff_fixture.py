#!/usr/bin/env python3
"""从大快照 tree.csv 流式生成改版快照目录（用于 diff 性能验证）。

改动（四组互不相交，各 500 行，按数据行序号选取）：
  - 行 1–500     删除
  - 行 501–1000  size += 7
  - 行 1001–1500 mtime +1s
  - 追加 500 个新路径 zzz_added/added_NNNN.bin

用法：python make_diff_fixture.py <src_dir> <dest_dir>
src/dest 均为 v1 快照目录（含 metadata/tree/warnings.csv）；metadata 中
collect_time_utc +1h 以产生不同的快照 ts（同 volume_id，构成同一盘的第二次快照）。
"""

import csv
import sys
from datetime import datetime, timedelta
from pathlib import Path

N_EACH = 500


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    src, dest = Path(sys.argv[1]), Path(sys.argv[2])
    dest.mkdir(parents=True, exist_ok=True)

    # metadata.csv：collect_time_utc +1h，其余原样
    with (src / "metadata.csv").open(newline="", encoding="utf-8-sig") as f:
        meta_rows = list(csv.reader(f))
    with (dest / "metadata.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for row in meta_rows:
            if len(row) >= 2 and row[0] == "collect_time_utc":
                dt = datetime.strptime(row[1].strip(), "%Y-%m-%dT%H:%M:%SZ") + timedelta(hours=1)
                row = [row[0], dt.strftime("%Y-%m-%dT%H:%M:%SZ")]
            w.writerow(row)
    (dest / "warnings.csv").write_bytes((src / "warnings.csv").read_bytes())

    with (src / "tree.csv").open(newline="", encoding="utf-8-sig") as fin, \
         (dest / "tree.csv").open("w", newline="", encoding="utf-8") as fout:
        r = csv.reader(fin)
        w = csv.writer(fout)
        header = next(r)
        w.writerow(header)
        i_size = header.index("size_bytes")
        i_mtime = header.index("modified_utc")
        i_path = header.index("relative_path")
        i_name = header.index("entry_name")

        def bump_mtime(iso: str) -> str:
            dt = datetime.strptime(iso.strip(), "%Y-%m-%dT%H:%M:%SZ") + timedelta(seconds=1)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        n = 0
        for row in r:
            n += 1
            if n <= N_EACH:
                continue                    # 删除 1–500
            if N_EACH < n <= 2 * N_EACH:
                row = list(row)
                row[i_size] = str(int(row[i_size]) + 7)   # 改 size
            elif 2 * N_EACH < n <= 3 * N_EACH:
                row = list(row)
                row[i_mtime] = bump_mtime(row[i_mtime])   # 改 mtime
            w.writerow(row)

        # 追加 500 个新路径（排最后，不影响流式生成；导入按 path 建 uq 索引）
        vid = header[0]
        for i in range(N_EACH):
            rel = f"zzz_added/added_{i:04d}.bin"
            w.writerow([vid, rel, f"added_{i:04d}.bin", "file", str(1000 + i),
                        "2026-06-01T00:00:00Z", "zzz_added", f"added_{i:04d}.bin"])

    print(f"改版快照已生成：{dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
