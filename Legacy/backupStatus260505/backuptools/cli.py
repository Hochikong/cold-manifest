"""命令行入口：collect 与 compare 子命令。"""

import argparse
import logging
import sys

from backuptools import __version__
from backuptools.utils import setup_logging

logger = logging.getLogger(__name__)


def _validate_depth(depth: int, tree_path: str):
    """校验比对深度不超出 tree.csv 实际最大深度。"""
    import csv
    max_depth = 0
    with open(tree_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rel = row.get("relative_path", "")
            if rel and rel != ".":
                d = rel.count("/") + 1
                if d > max_depth:
                    max_depth = d
    if depth > max_depth:
        print(
            f"error: depth {depth} exceeds max depth {max_depth} "
            f"in {tree_path}",
            file=sys.stderr,
        )
        sys.exit(1)


def cmd_collect(args):
    """处理 collect 子命令。"""
    from backuptools.collector import collect

    drive = args.drive.rstrip(":\\") + ":"
    collect(
        drive=drive,
        output_dir=args.output_dir,
        serial=args.serial,
        exclude_globs=args.exclude_glob or None,
        exclude_hidden=args.exclude_hidden,
        include_system=args.include_system,
        no_progress=args.no_progress,
    )


def cmd_compare(args):
    """处理 compare 子命令。"""
    from backuptools.comparator import compare

    # 校验深度
    import os
    for snap, label in [(args.snapshot_a, "A"), (args.snapshot_b, "B")]:
        tree = os.path.join(snap, "tree.csv")
        if not os.path.isfile(tree):
            print(f"error: tree.csv not found in {snap}", file=sys.stderr)
            sys.exit(1)
        _validate_depth(args.depth, tree)

    compare(
        snapshot_a=args.snapshot_a,
        snapshot_b=args.snapshot_b,
        depth=args.depth,
        output=args.output,
        show_identical=args.show_identical,
        ignore_mtime=args.ignore_mtime,
        ignore_size=args.ignore_size,
        temp_dir=args.temp_dir,
    )


def main():
    parser = argparse.ArgumentParser(
        prog="backuptools",
        description="冷备份磁盘数据采集与比对工具",
    )
    parser.add_argument(
        "--version", action="version",
        version=f"backuptools {__version__}",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="详细日志输出",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # ---------- collect ----------
    p_collect = sub.add_parser("collect", help="采集磁盘分区数据快照")
    p_collect.add_argument("drive", help="盘符，如 D: 或 E:")
    p_collect.add_argument(
        "-o", "--output-dir", default="./data",
        help="输出根目录 (默认 ./data)",
    )
    p_collect.add_argument(
        "--exclude-glob", action="append", default=None,
        help="排除路径模式 (glob)，可重复指定",
    )
    p_collect.add_argument(
        "--exclude-hidden", action="store_true", default=False,
        help="排除隐藏文件",
    )
    p_collect.add_argument(
        "--include-system", action="store_true", default=False,
        help="包含系统文件",
    )
    p_collect.add_argument(
        "--no-progress", action="store_true", default=False,
        help="关闭进度显示",
    )
    p_collect.add_argument(
        "-s", "--serial", default=None,
        help="磁盘序列号（手动指定，如 CrystalDiskInfo 中读取的值）",
    )

    # ---------- compare ----------
    p_compare = sub.add_parser("compare", help="比对两份数据快照")
    p_compare.add_argument("snapshot_a", help="采集目录 A")
    p_compare.add_argument("snapshot_b", help="采集目录 B")
    p_compare.add_argument(
        "-d", "--depth", type=int, required=True,
        help="比对层级深度 (>=0)",
    )
    p_compare.add_argument(
        "-o", "--output", default="diff.csv",
        help="差异 CSV 输出路径 (默认 diff.csv)",
    )
    p_compare.add_argument(
        "--show-identical", action="store_true", default=False,
        help="输出完全一致项",
    )
    p_compare.add_argument(
        "--ignore-mtime", action="store_true", default=False,
        help="忽略修改时间差异",
    )
    p_compare.add_argument(
        "--ignore-size", action="store_true", default=False,
        help="忽略文件大小差异",
    )
    p_compare.add_argument(
        "--temp-dir", default=None,
        help="外部排序临时目录",
    )

    args = parser.parse_args()
    setup_logging(args.verbose)

    if args.command == "collect":
        cmd_collect(args)
    elif args.command == "compare":
        cmd_compare(args)


if __name__ == "__main__":
    main()
