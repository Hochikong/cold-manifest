"""文件目录树扫描引擎：两遍扫描，第一遍测深度，第二遍写出 CSV。"""

import csv
import logging
import os
from typing import Callable, Optional

from backuptools.utils import (
    add_long_prefix,
    is_hidden,
    is_reparse_point,
    is_system,
    match_glob_patterns,
    relative_path_components,
    strip_long_prefix,
    timestamp_to_utc_iso,
)
from backuptools.csvschema import make_tree_row, tree_header

logger = logging.getLogger(__name__)


def scan_tree(
    root: str,
    volume_id: str,
    output_path: str,
    warnings_path: str,
    include_system: bool = False,
    exclude_hidden: bool = False,
    exclude_globs: Optional[list] = None,
    progress_callback: Optional[Callable] = None,
):
    """两遍扫描目录树并写出 tree.csv，同时记录 warnings.csv。

    第一遍：快速遍历，计算最大深度和条目数。
    第二遍：完整扫描，写出 CSV 行。
    """
    root = os.path.normpath(root)
    long_root = add_long_prefix(root)
    exclude_globs = exclude_globs or []

    # 打开 warnings 文件
    warn_file = open(warnings_path, "w", newline="", encoding="utf-8")
    warn_writer = csv.DictWriter(
        warn_file,
        fieldnames=["stage", "path", "warning_type", "detail"],
    )
    warn_writer.writeheader()

    def skip_entry(full_path: str) -> Optional[str]:
        """检查条目是否应跳过。返回跳过原因字符串，不跳过返回 None。"""
        try:
            if is_reparse_point(full_path):
                return "reparse_point_skipped"
            if include_system and not exclude_hidden:
                return None
            if not include_system and is_system(full_path):
                return "system_file_skipped"
            if exclude_hidden and is_hidden(full_path):
                return "hidden_file_skipped"
            return None
        except OSError:
            return None

    def log_warning(stage: str, path: str, wtype: str, detail: str = ""):
        warn_writer.writerow({
            "stage": stage,
            "path": path,
            "warning_type": wtype,
            "detail": detail,
        })

    # ====== 第一遍：测量最大深度和条目数 ======
    logger.info("Pass 1: measuring directory depth...")
    max_depth = 0
    total_entries = 0
    scanned = 0

    for dirpath, dirnames, filenames in os.walk(long_root, topdown=True):
        scanned += 1

        # 过滤并剪枝
        kept_dirs = []
        for d in dirnames:
            full = os.path.join(dirpath, d)
            skip = skip_entry(full)
            if skip:
                log_warning("scan", strip_long_prefix(full), skip)
                continue
            kept_dirs.append(d)
            depth = len(relative_path_components(root, full))
            if depth > max_depth:
                max_depth = depth
        dirnames[:] = kept_dirs

        for f in filenames:
            full = os.path.join(dirpath, f)
            skip = skip_entry(full)
            if skip:
                log_warning("scan", strip_long_prefix(full), skip)
                continue
            depth = len(relative_path_components(root, full))
            if depth > max_depth:
                max_depth = depth

        # 空目录
        if not kept_dirs and not [
            x for x in filenames
            if not skip_entry(os.path.join(dirpath, x))
        ]:
            depth = len(relative_path_components(root, dirpath))
            if depth > max_depth:
                max_depth = depth

        total_entries += len(kept_dirs) + len(filenames)

        if progress_callback:
            progress_callback(dirpath, 0)
    # 确保 level 列至少为 1
    if max_depth < 1:
        max_depth = 1

    logger.info(
        "Pass 1 done: max_depth=%d, scanned_dirs=%d",
        max_depth, scanned,
    )

    # ====== 第二遍：写出数据 ======
    logger.info("Pass 2: writing tree.csv...")
    header = tree_header(max_depth)

    with open(output_path, "w", newline="", encoding="utf-8") as out_file:
        writer = csv.DictWriter(out_file, fieldnames=header)
        writer.writeheader()

        file_count = 0
        dir_count = 0
        skip_count = 0

        for dirpath, dirnames, filenames in os.walk(long_root, topdown=True):
            # 过滤并剪枝
            kept_dirs = []
            for d in dirnames:
                full = os.path.join(dirpath, d)
                skip = skip_entry(full)
                if skip:
                    log_warning("scan", strip_long_prefix(full), skip)
                    continue
                kept_dirs.append(d)
            dirnames[:] = kept_dirs

            # 当前目录的相对路径
            rel = "/".join(relative_path_components(root, dirpath))
            dir_st = os.stat(dirpath)

            # 判断当前目录是否为空（过滤后无子项）
            visible_files = []
            for f in filenames:
                full = os.path.join(dirpath, f)
                skip = skip_entry(full)
                if skip:
                    log_warning("scan", strip_long_prefix(full), skip)
                    skip_count += 1
                    continue
                visible_files.append(f)

            if not kept_dirs and not visible_files:
                # 空目录
                rel_parts = relative_path_components(root, dirpath)
                entry_name = rel_parts[-1] if rel_parts else ""
                row_rel = "/".join(rel_parts) if rel_parts else "."
                writer.writerow(make_tree_row(
                    volume_id=volume_id,
                    relative_path=row_rel,
                    entry_name=entry_name,
                    entry_type="empty_dir",
                    size_bytes=0,
                    modified_utc=timestamp_to_utc_iso(dir_st.st_mtime),
                    levels=_pad_levels(rel_parts, max_depth),
                ))
                dir_count += 1

            # 文件
            for f in visible_files:
                full = os.path.join(dirpath, f)
                try:
                    st = os.stat(full)
                except (PermissionError, FileNotFoundError) as e:
                    log_warning(
                        "scan",
                        strip_long_prefix(full),
                        type(e).__name__.lower(),
                        str(e),
                    )
                    skip_count += 1
                    continue

                rel_parts = relative_path_components(root, full)
                entry_name = rel_parts[-1]
                row_rel = "/".join(rel_parts)
                writer.writerow(make_tree_row(
                    volume_id=volume_id,
                    relative_path=row_rel,
                    entry_name=entry_name,
                    entry_type="file",
                    size_bytes=st.st_size,
                    modified_utc=timestamp_to_utc_iso(st.st_mtime),
                    levels=_pad_levels(rel_parts, max_depth),
                ))
                file_count += 1

            if progress_callback:
                progress_callback(dirpath, file_count + dir_count)

    warn_file.close()
    logger.info(
        "Pass 2 done: files=%d, empty_dirs=%d, skipped=%d",
        file_count, dir_count, skip_count,
    )


def _pad_levels(parts: list, max_depth: int) -> list:
    """将路径层级补齐到 max_depth，空缺用空字符串填充。"""
    padded = list(parts)
    while len(padded) < max_depth:
        padded.append("")
    return padded
