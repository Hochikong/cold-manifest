"""cldm 命令行入口。"""

import argparse
import csv
import os
import sys
from pathlib import Path

from . import __version__
from .exporter import export_csv as _export_csv
from .exporter import export_v1_csv as _export_v1_csv
from .exporter import open_snapshot_rwcheck as _open_snapshot_rwcheck


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cldm",
        description="cold-manifest：冷备份磁盘元数据采集与比对",
    )
    parser.add_argument("--version", action="version", version=f"cldm {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<命令>")

    p_serve = sub.add_parser("serve", help="启动本地 Web 服务")
    p_serve.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0，仅本机用 127.0.0.1）")
    p_serve.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    p_serve.add_argument("--data-root", default=None, help="数据根目录（默认取环境变量 CLDM_DATA_ROOT，否则 ./data）")

    p_import = sub.add_parser("import-legacy", help="导入 v1 旧版快照目录（metadata.csv）")
    p_import.add_argument("snapshot_dir", help="旧版快照目录路径")
    p_import.add_argument("--data-root", default="./data", help="数据根目录（默认 ./data）")
    p_import.add_argument("--volume-id", default=None, help="手动指定卷 ID（默认从元数据推断）")
    p_import.add_argument("--force", action="store_true", help="目标已存在封库快照时删除重建")

    p_diff = sub.add_parser("diff", help="比对两个快照（同步执行，退出码 0 无差异 / 1 有差异 / 2 错误）")
    p_diff.add_argument("a", help="快照 A（快照 ID，如 VOL_P0/20260101T000000Z）")
    p_diff.add_argument("b", help="快照 B")
    p_diff.add_argument("--depth", type=int, default=None, help="结果按目录深度过滤（查询期，不影响物化）")
    p_diff.add_argument("--output", default=None, help="diff 结果写 CSV（category,path,type,size_a,size_b,mtime_a,mtime_b）")
    p_diff.add_argument("--data-root", default="./data", help="数据根目录（默认 ./data）")

    p_export = sub.add_parser("export", help="导出快照为 CSV")
    p_export.add_argument("snapshot", help="快照 ID 或路径")
    p_export.add_argument("--format", choices=["csv", "v1_csv"], default="csv", help="导出格式（默认 csv）")
    p_export.add_argument("--output", default=None,
                          help="csv：输出文件路径；v1_csv：输出目录（产出 metadata/tree/warnings 三件套）")
    p_export.add_argument("--data-root", default="./data", help="数据根目录（默认 ./data）")

    return parser


# ---- diff -----------------------------------------------------------------

_DIFF_CSV_HEADER = ["category", "path", "type", "size_a", "size_b", "mtime_a", "mtime_b"]


def _print_diff_summary(counts: dict) -> None:
    print("分类计数：")
    for k in ("added", "removed", "type_changed", "size_changed",
              "mtime_changed", "content_changed", "moved_or_renamed", "identical"):
        v = counts.get(k)
        print(f"  {k:>18}: {'—' if v is None else format(v, ',')}")


def _cmd_diff(args: argparse.Namespace) -> int:
    from .diff_engine import DiffError, iter_diff_csv, materialize_diff

    try:
        result = materialize_diff(args.data_root, args.a, args.b)
    except DiffError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2

    tag = "（复用既有结果库）" if result.reused else ""
    print(f"diff 完成：{result.diff_id}{tag}")
    print(f"  A：{args.a}")
    print(f"  B：{args.b}")
    print(f"  耗时：{result.elapsed_s:.1f}s  结果库：{result.db_path}")
    _print_diff_summary(result.counts)

    if args.output is not None:
        rows = 0
        with open(args.output, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(_DIFF_CSV_HEADER)
            for row in iter_diff_csv(result.db_path, depth=args.depth):
                w.writerow(tuple(row))
                rows += 1
        print(f"  已写出 {rows:,} 行 → {args.output}")

    changed = sum(v for k, v in result.counts.items() if k != "identical" and v)
    return 1 if changed else 0


# ---- export ---------------------------------------------------------------
# 实现提取到 exporter.py（CLI 与 Web API 共用）


def _cmd_export(args: argparse.Namespace) -> int:
    conn = _open_snapshot_rwcheck(args.data_root, args.snapshot)
    try:
        if args.format == "csv":
            out = Path(args.output) if args.output else Path(f"{args.snapshot.replace('/', '_')}.csv")
            out.parent.mkdir(parents=True, exist_ok=True)
            _export_csv(conn, out)
            print(f"导出完成：{out}")
        else:
            if not args.output:
                print("错误：--format v1_csv 需要 --output 指定输出目录", file=sys.stderr)
                return 2
            _export_v1_csv(conn, Path(args.output))
            print(f"导出完成（v1 三件套）→ {args.output}/：metadata.csv tree.csv warnings.csv")
        return 0
    finally:
        conn.close()


# ---- main -------------------------------------------------------------------

def main(argv: "list[str] | None" = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "serve":
        import uvicorn

        if args.data_root is not None:
            os.environ["CLDM_DATA_ROOT"] = str(Path(args.data_root).resolve())
        uvicorn.run("cold_manifest.server:app", host=args.host, port=args.port)
        return 0

    if args.command == "import-legacy":
        from .import_legacy import LegacyImportError, import_snapshot

        try:
            result = import_snapshot(args.snapshot_dir, args.data_root,
                                     volume_id=args.volume_id, force=args.force)
        except LegacyImportError as e:
            print(f"错误：{e}", file=sys.stderr)
            return 2
        if result.skipped_import:
            print(f"跳过：{result.snapshot_id} 已存在封库快照（--force 可重建）")
            print(f"路径：{result.db_path}")
            return 0
        s = result.stats
        print(f"导入完成：{result.snapshot_id}")
        print(f"  文件：{s['file_count']:,}  目录：{s['dir_count']:,}  跳过：{s['skipped_count']:,}")
        print(f"  总字节：{s['total_bytes']:,}（{s['total_bytes'] / 2**30:.2f} GiB）")
        print(f"  最深层级：{s['max_depth']}")
        print(f"  耗时：{result.elapsed_s:.1f}s  库大小：{result.db_size:,} B"
              f"（{result.db_size / 2**20:.1f} MiB）")
        print(f"  路径：{result.db_path}")
        return 0

    if args.command == "diff":
        return _cmd_diff(args)

    if args.command == "export":
        try:
            return _cmd_export(args)
        except FileNotFoundError as e:
            print(f"错误：{e}", file=sys.stderr)
            return 2

    print(f"错误：未知子命令 '{args.command}'", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
