"""cldm 命令行入口。"""

import argparse
import csv
import os
import sqlite3
import sys
import time
from pathlib import Path

from . import __version__
from .exporter import export_csv as _export_csv
from .exporter import export_v1_csv as _export_v1_csv
from .exporter import open_snapshot_rwcheck as _open_snapshot_rwcheck


def _default_data_root() -> str:
    """--data-root 默认值：环境变量 CLDM_DATA_ROOT 优先，否则 ./data。

    与 serve / start.cmd / cldm.cmd 的口径保持一致，保证包内 CLI 与
    Web 服务默认使用同一个数据根（<包根>\\data）。
    """
    return os.environ.get("CLDM_DATA_ROOT") or "./data"


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

    p_collect = sub.add_parser("collect", help="采集一个卷：probe → 扫描 → 封库 → 盘上副本 → 注册")
    p_collect.add_argument("root", help="扫描根目录（卷挂载点/盘符）")
    p_collect.add_argument("--data-root", default=_default_data_root(), help="数据根目录（默认取环境变量 CLDM_DATA_ROOT，否则 ./data）")
    p_collect.add_argument("--volume-id", default=None, help="手动指定卷 ID（默认 {serial}_P{index}）")
    p_collect.add_argument("--serial", default=None, help="手动指定磁盘序列号（USB 桥兜底）")
    p_collect.add_argument("--exclude-glob", action="append", default=[], dest="exclude_globs",
                           metavar="GLOB", help="排除 glob（可多次；目录命中整棵剪枝）")
    p_collect.add_argument("--exclude-hidden", action="store_true", help="跳过隐藏文件/目录")
    p_collect.add_argument("--no-smartctl", action="store_true", help="不调用 smartctl")
    p_collect.add_argument("--no-on-disk-copy", action="store_true", help="不写盘上副本")
    p_collect.add_argument("--resume", action="store_true",
                           help="断点续采：续接该卷最新的未封库采集（scan_journal），找不到则新建")
    p_collect.add_argument("--cross-filesystems", action="store_true",
                           help="跨入子挂载点（默认剪枝其他文件系统，如 /proc、/sys、网络盘）")
    p_collect.add_argument("--all-partitions", action="store_true",
                           help="把 root 视为盘/挂载点，逐个采集该盘全部分区（串行，批次部分失败=partial）")

    p_import = sub.add_parser("import-legacy", help="导入 v1 旧版快照目录（metadata.csv）")
    p_import.add_argument("snapshot_dir", help="旧版快照目录路径")
    p_import.add_argument("--data-root", default=_default_data_root(), help="数据根目录（默认取环境变量 CLDM_DATA_ROOT，否则 ./data）")
    p_import.add_argument("--volume-id", default=None, help="手动指定卷 ID（默认从元数据推断）")
    p_import.add_argument("--force", action="store_true", help="目标已存在封库快照时删除重建")

    p_diff = sub.add_parser("diff", help="比对两个快照（同步执行，退出码 0 无差异 / 1 有差异 / 2 错误）")
    p_diff.add_argument("a", help="快照 A（快照 ID，如 VOL_P0/20260101T000000Z）")
    p_diff.add_argument("b", help="快照 B")
    p_diff.add_argument("--depth", type=int, default=None, help="结果按目录深度过滤（查询期，不影响物化）")
    p_diff.add_argument("--hash", choices=["none", "sha256"], default="none",
                        help="内容比对模式：sha256 需两快照已 cldm hash，可识别 content_changed / moved_or_renamed")
    p_diff.add_argument("--output", default=None, help="diff 结果写 CSV（category,path,type,size_a,size_b,mtime_a,mtime_b）")
    p_diff.add_argument("--data-root", default=_default_data_root(), help="数据根目录（默认取环境变量 CLDM_DATA_ROOT，否则 ./data）")

    p_export = sub.add_parser("export", help="导出快照为 CSV")
    p_export.add_argument("snapshot", help="快照 ID 或路径")
    p_export.add_argument("--format", choices=["csv", "v1_csv"], default="csv", help="导出格式（默认 csv）")
    p_export.add_argument("--output", default=None,
                          help="csv：输出文件路径；v1_csv：输出目录（产出 metadata/tree/warnings 三件套）")
    p_export.add_argument("--data-root", default=_default_data_root(), help="数据根目录（默认取环境变量 CLDM_DATA_ROOT，否则 ./data）")

    p_fts = sub.add_parser("build-fts",
                           help="为已有封库快照就地补建 FTS5 全文索引（显式升级动作）")
    p_fts.add_argument("snapshot_id", help="快照 ID（如 VOL_P0/20260101T000000Z）或 snapshot.db 路径")
    p_fts.add_argument("--data-root", default=_default_data_root(), help="数据根目录（默认取环境变量 CLDM_DATA_ROOT，否则 ./data）")

    p_stats = sub.add_parser("build-stats",
                             help="为已有封库快照就地补建 stats 预计算表（显式升级动作）")
    p_stats.add_argument("snapshot_id", help="快照 ID（如 VOL_P0/20260101T000000Z）或 snapshot.db 路径")
    p_stats.add_argument("--data-root", default=_default_data_root(), help="数据根目录（默认取环境变量 CLDM_DATA_ROOT，否则 ./data）")

    p_hash = sub.add_parser("hash",
                            help="为快照补算文件哈希（按需，可续算，跨快照缓存复用；"
                                 "缓存键不含 volume_id，跨盘同名同大小同 mtime 可能复用旧哈希）")
    p_hash.add_argument("snapshot_id", help="快照 ID（如 VOL_P0/20260101T000000Z）")
    p_hash.add_argument("--data-root", default=_default_data_root(), help="数据根目录（默认取环境变量 CLDM_DATA_ROOT，否则 ./data）")
    p_hash.add_argument("--algo", default="sha256", help="哈希算法（默认 sha256）")
    p_hash.add_argument("--policy", choices=["full", "sampled"], default="full",
                        help="哈希策略：full=整文件（默认，diff 内容比对可用）；"
                             "sampled=首/中/尾各 64KB 指纹 v2（不能证明内容等值；"
                             "旧 v1 指纹仍按 sampled 档参与查重）")
    p_hash.add_argument("--candidates", action="store_true",
                        help="只哈希『大小在本快照出现 ≥2 次且 >0』的候选文件"
                             "（配合查重：非候选不哈希；meta 记 hash_scope=candidates）")
    p_hash.add_argument("--group", default=None, metavar="HASH_HEX",
                        help="只对当前 hash_hex 命中该值的条目做全量精验"
                             "（强制 full 策略，配合 duplicates --mode fingerprint；"
                             "与 --candidates 互斥）")
    p_hash.add_argument("--limit", type=int, default=None,
                        help="调试：本次最多处理 N 个选中条目（按 entry_id 升序；"
                             "对 --group/--candidates 同样适用，默认不限）")
    p_hash.add_argument("--root", default=None,
                        help="显式指定源目录（覆盖 catalog 记录的 host_path，须为目录）")

    p_dups = sub.add_parser("duplicates",
                            help="快照内重复文件三档查重"
                                 "（--mode content=完整哈希 / name=同名 /"
                                 " fingerprint=大小+指纹）")
    p_dups.add_argument("snapshot_id", help="快照 ID（如 VOL_P0/20260101T000000Z）或 snapshot.db 路径")
    p_dups.add_argument("--mode", choices=["content", "name", "fingerprint"],
                        default="content",
                        help="查重档位：content=完整哈希分组（默认，要求已"
                             " cldm hash --policy full）；name=同名分组"
                             "（大小写不敏感，无需哈希，秒级）；fingerprint=按"
                             " (大小,哈希) 分组（纳入 full/sampled 指纹，未逐字节"
                             "验证；无任何哈希时报错）")
    p_dups.add_argument("--data-root", default=_default_data_root(), help="数据根目录（默认取环境变量 CLDM_DATA_ROOT，否则 ./data）")
    p_dups.add_argument("--min-size", type=int, default=1048576,
                        help="只统计 ≥ 此字节数的文件（默认 1048576 = 1MiB）")
    p_dups.add_argument("--limit", type=int, default=100, help="列出前 N 组（默认 100，上限 1000）")
    p_dups.add_argument("--output", choices=["text", "csv"], default="text",
                        help="text=摘要（默认）；csv=全字段清单")
    p_dups.add_argument("--html", default=None, help="另存自包含 HTML 报告到指定路径")

    p_delete = sub.add_parser("delete",
                              help="删除快照：主机快照目录 +（可选）盘上副本 + catalog 注册行"
                                   "（退出码 0 成功 / 2 错误或被阻塞）")
    p_delete.add_argument("snapshot_id", help="快照 ID（如 VOL_P0/20260101T000000Z）")
    p_delete.add_argument("--data-root", default=_default_data_root(), help="数据根目录（默认取环境变量 CLDM_DATA_ROOT，否则 ./data）")
    p_delete.add_argument("--on-disk", choices=["keep", "delete"], default="keep",
                          help="盘上副本处置：keep=保留（默认）；delete=同时删除"
                               " <scan_root>/_coldmanifest/<volume_id>/<ts>/")
    p_delete.add_argument("--force", action="store_true",
                          help="快照被 diff 引用时级联删除这些 diff（物化库 + catalog 行）")

    p_verify = sub.add_parser("verify-copy",
                              help="校验快照盘上副本与源文件完整性（只读；退出码 0 一致 / 1 不一致 / 2 错误）")
    p_verify.add_argument("snapshot_id", help="快照 ID（如 VOL_P0/20260101T000000Z）")
    p_verify.add_argument("--data-root", default=_default_data_root(), help="数据根目录（默认取环境变量 CLDM_DATA_ROOT，否则 ./data）")
    p_verify.add_argument("--sample", type=int, default=0,
                          help="源文件随机抽检条数（需快照已 cldm hash --policy full；默认 0 = 只对副本）")
    p_verify.add_argument("--full", action="store_true",
                          help="抽检全部已哈希文件（可能很慢）")
    p_verify.add_argument("--seed", type=int, default=None, help="抽样随机种子（可复现）")
    p_verify.add_argument("--root", default=None,
                          help="显式指定扫描根（覆盖 meta/catalog 记录，须为目录）")

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
        result = materialize_diff(args.data_root, args.a, args.b,
                                  options={"hash": args.hash})
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


def _print_collect_result(result) -> None:
    print(f"  文件：{result.files:,}  目录：{result.dirs:,}  符号链接：{result.symlinks:,}"
          f"  跳过：{result.skipped:,}")
    print(f"  总字节：{result.total_bytes:,}（{result.total_bytes / 2**30:.2f} GiB）")
    print(f"  耗时：{result.elapsed_s:.1f}s  吞吐："
          f"{(result.files + result.dirs) / result.elapsed_s:,.0f} 条目/s")
    print(f"  快照库：{result.db_path}")
    print(f"  主机 sha256：{result.host_sha256}")
    if result.on_disk_path is not None:
        print(f"  盘上副本：{result.on_disk_path}（sha256 一致：{result.on_disk_sha256}）")
    for w in result.warnings:
        print(f"  警告：{w}")


def _cmd_collect_all_partitions(args: argparse.Namespace) -> int:
    """多分区批次采集（§4.2）：逐卷串行 collect_volume，打印每卷摘要与批次结果。"""
    from .collect import CollectError, collect_volume
    from .lockfile import DataRootLock, LockBusy
    from .probe import ProbeError, enumerate_disk_volumes

    lock = DataRootLock(args.data_root)
    try:
        lock.acquire()
    except LockBusy as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2
    try:
        return _collect_all_partitions_locked(args)
    finally:
        lock.release()


def _collect_all_partitions_locked(args: argparse.Namespace) -> int:
    from .collect import CollectError, collect_volume
    from .probe import ProbeError, enumerate_disk_volumes

    try:
        targets, warnings = enumerate_disk_volumes(args.root)
    except ProbeError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2
    for w in warnings:
        print(f"警告：{w}")
    if not targets:
        print("错误：该盘没有可采集的卷", file=sys.stderr)
        return 2

    print(f"批次共 {len(targets)} 个卷：")
    for i, t in enumerate(targets, 1):
        idx = t.partition_index if t.partition_index is not None else "?"
        print(f"  [{i}] {t.path}（{t.device_path or t.mount_point}，"
              f"分区 #{idx}，{t.filesystem or '?'}{'，' + t.label if t.label else ''}）")

    statuses: list[tuple[str, str]] = []
    for t in targets:
        print(f"\n== 采集 {t.path} ==")
        try:
            result = collect_volume(
                t.path, data_root=args.data_root, manual_serial=args.serial,
                exclude_globs=args.exclude_globs, exclude_hidden=args.exclude_hidden,
                smartctl=not args.no_smartctl, on_disk_copy=not args.no_on_disk_copy,
                resume=args.resume,
                cross_filesystems=args.cross_filesystems,
                progress_cb=lambda phase, done, total: None,
            )
        except (CollectError, ProbeError) as e:
            print(f"  失败：{e}", file=sys.stderr)
            statuses.append((t.path, f"error: {e}"))
            continue
        print(f"采集完成：{result.snapshot_id}")
        _print_collect_result(result)
        statuses.append((t.path, "done"))

    print("\n== 批次结果 ==")
    done = sum(1 for _, s in statuses if s == "done")
    failed = len(statuses) - done
    overall = "done" if failed == 0 else "partial"
    for path, s in statuses:
        print(f"  {path}: {s}")
    print(f"批次状态：{overall}（成功 {done}/{len(statuses)}）")
    return 0 if failed == 0 else 1


def _cmd_collect(args: argparse.Namespace) -> int:
    if getattr(args, "all_partitions", False):
        return _cmd_collect_all_partitions(args)

    from .collect import CollectError, collect_volume
    from .lockfile import DataRootLock, LockBusy
    from .probe import ProbeError

    lock = DataRootLock(args.data_root)
    try:
        lock.acquire()
    except LockBusy as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2

    t0 = time.monotonic()

    def cb(phase: str, done: int, total: "int | None") -> None:
        if phase == "scan":
            sys.stdout.write(f"\r  扫描中：{done:,} 条目")
            sys.stdout.flush()

    try:
        result = collect_volume(
            args.root, data_root=args.data_root, volume_id=args.volume_id,
            manual_serial=args.serial, exclude_globs=args.exclude_globs,
            exclude_hidden=args.exclude_hidden, smartctl=not args.no_smartctl,
            on_disk_copy=not args.no_on_disk_copy, resume=args.resume,
            cross_filesystems=args.cross_filesystems,
            progress_cb=cb,
        )
    except (CollectError, ProbeError) as e:
        print(f"\n错误：{e}", file=sys.stderr)
        return 2
    finally:
        lock.release()
    print()
    print(f"采集完成：{result.snapshot_id}" + ("（断点续采）" if result.resumed else ""))
    _print_collect_result(result)
    return 0


# ---- build-fts -------------------------------------------------------------

def _cmd_build_fts(args: argparse.Namespace) -> int:
    """为已有封库快照就地构建 FTS5 全文索引（快照是不可变制品，此为显式升级动作）。

    幂等：entries_fts 已存在时 DROP 后整体重建，保证索引与 entries 严格一致。
    写入须绕过只读打开（open_snapshot 是 mode=ro&immutable），此处显式以读写打开。
    """
    from .catalog import snapshot_path
    from .seal import build_fts, fts_available

    db = Path(args.snapshot_id)
    if not db.is_file():
        db = snapshot_path(args.data_root, args.snapshot_id)
        if not db.is_file():
            print(f"错误：快照不存在：{args.snapshot_id}（{db}）", file=sys.stderr)
            return 2

    conn = sqlite3.connect(db.as_posix())
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='status'").fetchone()
        if not row or row[0] != "sealed":
            print("警告：该快照未封库（meta.status != sealed），建议先完成封库再建索引",
                  file=sys.stderr)
        existed = fts_available(conn)
        t0 = time.monotonic()
        count, tok = build_fts(conn)
        conn.commit()
        elapsed = time.monotonic() - t0
        print(f"{'重建' if existed else '构建'}完成：entries_fts（{tok} 分词）")
        print(f"  条目数：{count:,}  耗时：{elapsed:.1f}s")
        print(f"  库：{db}")
        if tok != "trigram":
            print("  警告：当前 SQLite 不支持 trigram 分词，已回退 unicode61"
                  "（中文子串匹配能力受限，建议 SQLite ≥3.34）", file=sys.stderr)
        return 0
    except sqlite3.Error as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2
    finally:
        conn.close()


# ---- build-stats ------------------------------------------------------------

def _cmd_build_stats(args: argparse.Namespace) -> int:
    """为已有封库快照就地补建 stats_precomputed（快照是不可变制品，显式升级动作）。

    幂等：整体重建（DROP 后重算重写），保证与 entries 严格一致。
    写入须绕过只读打开（open_snapshot 是 mode=ro&immutable），此处显式以读写打开。
    """
    from .catalog import snapshot_path
    from .stats_cache import build_stats_cache, stats_precomputed_available

    db = Path(args.snapshot_id)
    if not db.is_file():
        db = snapshot_path(args.data_root, args.snapshot_id)
        if not db.is_file():
            print(f"错误：快照不存在：{args.snapshot_id}（{db}）", file=sys.stderr)
            return 2

    conn = sqlite3.connect(db.as_posix())
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='status'").fetchone()
        if not row or row[0] != "sealed":
            print("警告：该快照未封库（meta.status != sealed），建议先完成封库再预计算",
                  file=sys.stderr)
        existed = stats_precomputed_available(conn)
        t0 = time.monotonic()
        n_keys = build_stats_cache(conn)
        conn.commit()
        elapsed = time.monotonic() - t0
        print(f"{'重建' if existed else '构建'}完成：stats_precomputed（{n_keys} 个聚合段）")
        print(f"  库：{db}  耗时：{elapsed:.1f}s")
        return 0
    except sqlite3.Error as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2
    finally:
        conn.close()


# ---- hash -------------------------------------------------------------------

def _cmd_hash(args: argparse.Namespace) -> int:
    """按需哈希（§4.5，同步执行）：持 data_root 写锁，可续算、缓存复用。"""
    from .catalog import connect_catalog, find_snapshot, snapshot_path
    from .hash import HashError, hash_snapshot
    from .lockfile import DataRootLock, LockBusy

    if args.candidates and args.group:
        print("错误：--group 与 --candidates 互斥：group 是对指定哈希组做全量"
              "精验，candidates 是批量补算大小候选集", file=sys.stderr)
        return 2

    data_root = Path(args.data_root)
    db = snapshot_path(data_root, args.snapshot_id)
    cat = connect_catalog(data_root)
    try:
        if find_snapshot(cat, args.snapshot_id) is None:
            print(f"错误：快照未注册：{args.snapshot_id}（数据根：{Path(args.data_root).resolve()}）\n"
                  f"若与 Web 服务的数据根不同，请用 --data-root 指定（或设 CLDM_DATA_ROOT）。",
                  file=sys.stderr)
            return 2
    finally:
        cat.close()
    if not db.is_file():
        print(f"错误：快照库文件缺失：{db}", file=sys.stderr)
        return 2

    lock = DataRootLock(data_root)
    try:
        lock.acquire()
    except LockBusy as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2
    try:
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            # 待哈希条目按选中范围计数（与 hash_snapshot 的选择一致）
            if args.group:
                pending = conn.execute(
                    "SELECT COUNT(*) FROM entries WHERE type='file' AND hash_hex=?",
                    (args.group,)).fetchone()[0]
                scope, group = "incremental", args.group
                if pending == 0:
                    print(f"没有 hash_hex 命中 {args.group} 的条目，无需精验")
                    return 0
                print(f"精验组 {args.group[:16]}…：{pending:,} 个条目  算法：{args.algo}"
                      f"  策略：full（强制）")
            else:
                scope = "candidates" if args.candidates else "incremental"
                group = None
                if args.candidates:
                    pending = conn.execute(
                        "SELECT COUNT(*) FROM entries WHERE type='file'"
                        " AND hash_hex IS NULL AND size_bytes > 0 AND size_bytes IN"
                        " (SELECT size_bytes FROM entries WHERE type='file'"
                        "  AND size_bytes > 0 GROUP BY size_bytes HAVING COUNT(*)>1)"
                    ).fetchone()[0]
                    if pending == 0:
                        print("候选集（大小出现 ≥2 次且 >0）内没有待哈希条目")
                        return 0
                    print(f"候选集待哈希条目：{pending:,}  算法：{args.algo}  策略：{args.policy}")
                else:
                    pending = conn.execute(
                        "SELECT COUNT(*) FROM entries WHERE type='file' AND hash_hex IS NULL"
                    ).fetchone()[0]
                    if pending == 0:
                        print("所有文件已有哈希，无需补算（如需换算法/策略请先清空 hash 列）")
                        return 0
                    print(f"待哈希条目：{pending:,}  算法：{args.algo}  策略：{args.policy}")
            result = hash_snapshot(
                conn, data_root, args.snapshot_id,
                algo=args.algo, policy=args.policy, limit=args.limit,
                root=args.root, scope=scope, group=group,
                progress_cb=lambda phase, done, total: (
                    sys.stdout.write(f"\r  {phase}:{done:,}/{total:,}") or sys.stdout.flush()
                ),
            )
        finally:
            conn.close()
    except (HashError, sqlite3.Error) as e:
        print(f"\n错误：{e}", file=sys.stderr)
        return 2
    finally:
        lock.release()

    print()
    print(f"哈希完成：{result['snapshot_id']}（范围：{result.get('scope', 'incremental')}）")
    print(f"  计算：{result['computed']:,}  缓存命中：{result['cached']:,}  "
          f"失败：{result['errors']:,}")
    mb = result["bytes_hashed"] / 2**20
    thr = (result["bytes_hashed"] / 2**20 / result["elapsed_s"]) if result["elapsed_s"] else 0.0
    print(f"  读取量：{mb:,.1f} MiB  耗时：{result['elapsed_s']:.1f}s  吞吐：{thr:,.1f} MiB/s")
    print(f"  源目录：{result['host_path']}")
    return 0


# ---- duplicates ---------------------------------------------------------------

def _cmd_duplicates(args: argparse.Namespace) -> int:
    """快照内重复文件报告（P3-A）：只认 full 哈希，按 wasted_bytes 降序列组。"""
    from .db import open_snapshot
    from .duplicates import (MAX_LIMIT, DuplicatesError,
                             duplicates_csv, find_duplicates,
                             render_duplicates_html)
    from .report import fmt_bytes

    db = Path(args.snapshot_id)
    if not db.is_file():
        from .catalog import snapshot_path
        db = snapshot_path(args.data_root, args.snapshot_id)
        if not db.is_file():
            print(f"错误：快照不存在：{args.snapshot_id}（{db}）", file=sys.stderr)
            return 2

    if not (1 <= args.limit <= MAX_LIMIT):
        print(f"错误：limit 须在 1..{MAX_LIMIT}", file=sys.stderr)
        return 2
    if args.min_size < 0:
        print("错误：min-size 不能为负", file=sys.stderr)
        return 2

    conn = open_snapshot(db)
    try:
        result = find_duplicates(conn, args.snapshot_id, mode=args.mode,
                                 min_size=args.min_size, limit=args.limit)
    except (DuplicatesError, ValueError) as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2
    finally:
        conn.close()

    if args.html:
        Path(args.html).write_text(render_duplicates_html(result, time.strftime(
            "%Y-%m-%d %H:%M:%S")), encoding="utf-8")
        print(f"HTML 报告：{args.html}")

    if args.output == "csv":
        sys.stdout.write(duplicates_csv(result))
        return 0

    # text 摘要
    mode = result["mode"]
    mode_lbl = {"content": "内容级（完整哈希）", "name": "同名级（大小写不敏感）",
                "fingerprint": "指纹级（大小+抽样指纹）"}[mode]
    print(f"重复文件报告：{result['snapshot_id']}（模式：{mode_lbl}）")
    wasted = result.get("total_wasted_bytes")
    wasted_txt = f"可回收空间：{fmt_bytes(wasted)}  " if wasted is not None else ""
    print(f"  重复组数：{result['duplicate_groups']:,}  {wasted_txt}"
          f"参与匹配文件：{result['hashed_files']:,}"
          f"（大小下限 {fmt_bytes(result['min_size'])}）")
    if mode == "fingerprint":
        print("  ⚠ 指纹级：未逐字节验证；对某一组严谨验证："
              "cldm hash <sid> --group <hash_hex>")
    elif mode == "name":
        print("  ⚠ 同名级：同名不代表内容相同。")
    if not result["items"]:
        print("  没有满足条件的重复文件。")
        return 0
    print(f"  前 {len(result['items'])} 组：")
    for i, it in enumerate(result["items"], 1):
        paths = "、".join(it["paths"][:3])
        if len(it["paths"]) > 3:
            paths += " …"
        if mode == "name":
            print(f"   {i:>3}. {it['count']} 个同名  "
                  f"共 {fmt_bytes(it['size_bytes']):<10}  {it['name']}  {paths}")
        else:
            ver = ""
            if mode == "fingerprint":
                ver = "  已验证" if it["verified"] else "  指纹未验证"
            print(f"   {i:>3}. 浪费 {fmt_bytes(it['wasted_bytes']):>10}  "
                  f"{it['count']} × {fmt_bytes(it['size_bytes']):<10} {paths}{ver}")
        if it["paths_truncated"]:
            print("        （路径仅列部分）")
    return 0


def _cmd_verify_copy(args: argparse.Namespace) -> int:
    """盘上副本对账 + 可选源文件抽检（只读，不持写锁）。

    副本基准是**采集时记录**的 sha256（catalog.on_disk_copies），因为采集后
    用户还可能跑 hash / build-fts / build-stats，那些都会改主机库；主机库与
    记录值不一致只作信息项（host_status），不计失败。
    退出码：0 全部一致 / 1 发现不一致 / 2 错误。
    """
    from .verify import VerifyError, verify_snapshot_copy

    try:
        result = verify_snapshot_copy(
            args.data_root, args.snapshot_id,
            sample=args.sample, seed=args.seed, full=args.full,
            root_override=args.root)
    except VerifyError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2

    print(f"校验：{result['snapshot_id']}  {'OK' if result['ok'] else '发现不一致'}")
    c = result["copy"]
    status_text = {
        "ok": "副本与采集记录一致",
        "mismatch": "副本与采集记录不一致（被改动或损坏）",
        "missing_disk": "盘上副本缺失",
        "not_recorded": "无采集时副本记录（import-legacy 或 --no-on-disk-copy），副本校验不可用",
    }.get(c["status"], c["status"])
    print(f"  副本：{status_text}")
    if c.get("recorded_sha256"):
        disk = (c.get("disk_sha256") or "—")[:16]
        print(f"        记录={c['recorded_sha256'][:16]}…  盘上={disk}…")
    if c.get("host_status") == "modified_since_collection":
        print("  主机库：采集后做过 hash/FTS/stats 等操作（属正常，不影响副本结论）")
    s = result["sidecar"]
    print(f"  旁车：{s['status']}")
    for p in s["problems"]:
        print(f"    - {p}")
    src = result["source"]
    if not src["available"]:
        print(f"  抽检：不可用（{src.get('error', '')}）")
    elif src["checked"]:
        print(f"  抽检：{src['checked']:,}  match={src['match']:,}  mismatch={src['mismatch']:,}"
              f"  missing={src['missing']:,}  unreadable={src['unreadable']:,}")
        for sm in src["samples"]:
            if sm["status"] != "match":
                exp = (sm.get("expected") or "—")[:12]
                act = (sm.get("actual") or "—")[:12]
                print(f"    [{sm['status']}] {sm['path']}  期望={exp}…  实际={act}…")
    return 0 if result["ok"] else 1


# ---- delete ------------------------------------------------------------------

def _cmd_delete(args: argparse.Namespace) -> int:
    """删除快照（P4-①）：持 data_root 写锁；404/被阻塞 → 退出码 2。"""
    from .catalog import (
        SnapshotDeleteBlocked,
        SnapshotDeleteError,
        connect_catalog,
        delete_snapshot,
    )
    from .lockfile import DataRootLock, LockBusy

    data_root = Path(args.data_root)
    if not data_root.is_dir():
        print(f"错误：数据根不存在：{data_root}", file=sys.stderr)
        return 2
    cat = connect_catalog(data_root)
    lock = DataRootLock(data_root)
    try:
        lock.acquire()
    except LockBusy as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2
    try:
        result = delete_snapshot(cat, data_root, args.snapshot_id,
                                 on_disk=args.on_disk, force=args.force)
    except LookupError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2
    except SnapshotDeleteError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2
    except SnapshotDeleteBlocked as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2
    finally:
        lock.release()
        cat.close()

    print(f"已删除快照：{result['snapshot_id']}")
    print(f"  主机目录：{'已删除' if result['deleted_host'] else '不存在（仅清理 catalog）'}")
    dd = result["deleted_disk"]
    print(f"  盘上副本：{'未处理（keep）' if dd is None else ('已删除' if dd else '未找到')}")
    print(f"  释放字节：{result['freed_bytes']:,}")
    if result["diffs_removed"]:
        print(f"  级联 diff：{', '.join(result['diffs_removed'])}")
    for w in result["warnings"]:
        print(f"  警告：{w}")
    return 0


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

    if args.command == "collect":
        return _cmd_collect(args)

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

    if args.command == "build-fts":
        return _cmd_build_fts(args)

    if args.command == "build-stats":
        return _cmd_build_stats(args)

    if args.command == "duplicates":
        return _cmd_duplicates(args)

    if args.command == "delete":
        return _cmd_delete(args)

    if args.command == "verify-copy":
        return _cmd_verify_copy(args)

    if args.command == "hash":
        return _cmd_hash(args)

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
