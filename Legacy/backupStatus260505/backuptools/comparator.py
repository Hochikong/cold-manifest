"""比对引擎：基于排序记录的流式双路归并差异比对。"""

import csv
import logging
import os
import tempfile

from backuptools.csvschema import DIFF_HEADER, make_diff_row
from backuptools.sorter import cleanup_chunks, merge_chunks, sort_tree_csv

logger = logging.getLogger(__name__)


def _read_volume_id(snapshot_dir: str) -> str:
    """从 metadata.csv 中读取 volume_id。"""
    meta_path = os.path.join(snapshot_dir, "metadata.csv")
    with open(meta_path, "r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("metric_name") == "volume_id":
                return row.get("value", "")
    return ""


def _safe_next(iterator):
    try:
        return next(iterator)
    except StopIteration:
        return None


def _sort_key(rec):
    """提取排序键：(compare_prefix, entry_name)。"""
    if rec is None:
        return ("\uffff", "\uffff")
    return (rec.get("compare_prefix", ""), rec.get("entry_name", ""))


def _classify(rec_a: dict, rec_b: dict, ignore_size: bool,
              ignore_mtime: bool) -> str:
    """判定两个条目的差异类型。"""
    et_a = rec_a.get("entry_type", "")
    et_b = rec_b.get("entry_type", "")

    if et_a != et_b:
        return "type_diff"

    if et_a == "file" and not ignore_size:
        sz_a = rec_a.get("size_bytes", "")
        sz_b = rec_b.get("size_bytes", "")
        if sz_a != sz_b:
            return "size_diff"

    if not ignore_mtime:
        mt_a = rec_a.get("modified_utc", "")
        mt_b = rec_b.get("modified_utc", "")
        if mt_a != mt_b:
            return "time_diff"

    return "identical"


def _record_cols(rec: dict) -> tuple:
    """提取记录的 (entry_type, size_bytes, modified_utc)。"""
    if rec is None:
        return ("", "", "")
    return (
        rec.get("entry_type", ""),
        rec.get("size_bytes", ""),
        rec.get("modified_utc", ""),
    )


def compare(
    snapshot_a: str,
    snapshot_b: str,
    depth: int,
    output: str,
    show_identical: bool = False,
    ignore_mtime: bool = False,
    ignore_size: bool = False,
    temp_dir: str = None,
):
    """比对两个采集快照在指定层级下的差异，输出 diff.csv。"""
    tree_a = os.path.join(snapshot_a, "tree.csv")
    tree_b = os.path.join(snapshot_b, "tree.csv")

    if not os.path.isfile(tree_a):
        raise FileNotFoundError(f"tree.csv not found: {tree_a}")
    if not os.path.isfile(tree_b):
        raise FileNotFoundError(f"tree.csv not found: {tree_b}")

    vid_a = _read_volume_id(snapshot_a)
    vid_b = _read_volume_id(snapshot_b)

    temp_dir = temp_dir or tempfile.gettempdir()
    if depth < 0:
        raise ValueError("depth must be >= 0")

    # 外部排序
    logger.info("Sorting snapshot A: %s", snapshot_a)
    chunks_a = sort_tree_csv(tree_a, depth, temp_dir)
    logger.info("Sorting snapshot B: %s", snapshot_b)
    chunks_b = sort_tree_csv(tree_b, depth, temp_dir)

    try:
        stream_a = merge_chunks(chunks_a)
        stream_b = merge_chunks(chunks_b)

        with open(output, "w", newline="", encoding="utf-8") as out:
            writer = csv.DictWriter(out, fieldnames=DIFF_HEADER)
            writer.writeheader()

            rec_a = _safe_next(stream_a)
            rec_b = _safe_next(stream_b)
            total = 0

            while rec_a is not None or rec_b is not None:
                key_a = _sort_key(rec_a)
                key_b = _sort_key(rec_b)

                if rec_a is not None and key_a == key_b:
                    # 两侧共有
                    diff = _classify(rec_a, rec_b, ignore_size, ignore_mtime)
                    if diff != "identical" or show_identical:
                        et_a, sz_a, mt_a = _record_cols(rec_a)
                        et_b, sz_b, mt_b = _record_cols(rec_b)
                        writer.writerow(make_diff_row(
                            prefix_path=rec_a["compare_prefix"],
                            entry_name=rec_a["entry_name"],
                            diff_type=diff,
                            entry_type_a=et_a,
                            entry_type_b=et_b,
                            size_a=sz_a,
                            size_b=sz_b,
                            modified_a=mt_a,
                            modified_b=mt_b,
                            volume_id_a=vid_a,
                            volume_id_b=vid_b,
                        ))
                        total += 1
                    rec_a = _safe_next(stream_a)
                    rec_b = _safe_next(stream_b)

                elif rec_b is None or (rec_a is not None and key_a < key_b):
                    # 仅 A 有
                    et, sz, mt = _record_cols(rec_a)
                    writer.writerow(make_diff_row(
                        prefix_path=rec_a["compare_prefix"],
                        entry_name=rec_a["entry_name"],
                        diff_type="only_in_a",
                        entry_type_a=et,
                        entry_type_b="",
                        size_a=sz,
                        size_b="",
                        modified_a=mt,
                        modified_b="",
                        volume_id_a=vid_a,
                        volume_id_b=vid_b,
                    ))
                    total += 1
                    rec_a = _safe_next(stream_a)

                else:
                    # 仅 B 有
                    et, sz, mt = _record_cols(rec_b)
                    writer.writerow(make_diff_row(
                        prefix_path=rec_b["compare_prefix"],
                        entry_name=rec_b["entry_name"],
                        diff_type="only_in_b",
                        entry_type_a="",
                        entry_type_b=et,
                        size_a="",
                        size_b=sz,
                        modified_a="",
                        modified_b=mt,
                        volume_id_a=vid_a,
                        volume_id_b=vid_b,
                    ))
                    total += 1
                    rec_b = _safe_next(stream_b)

        logger.info("Diff complete: %d rows -> %s", total, output)
    finally:
        cleanup_chunks(chunks_a)
        cleanup_chunks(chunks_b)
