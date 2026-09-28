"""超大 CSV 分块排序与归并：用于比对前的数据预处理。"""

import csv
import heapq
import logging
import os
import tempfile
from typing import Iterator, Optional

from backuptools.csvschema import NORMALIZED_HEADER, make_normalized_record

logger = logging.getLogger(__name__)

# 默认内存分块大小（行数）
DEFAULT_CHUNK_SIZE = 200_000


def _tree_rows_to_normalized(
    rows: Iterator[dict],
    depth: int,
) -> list:
    """将 tree.csv 行转为标准化比对记录，去重后返回列表。

    depth=0: 比对根层，compare_prefix=""，entry_name=parts[0]。
    depth=N: 比对 level_N 下的直接子项，compare_prefix=parts[:N]，entry_name=parts[N]。

    非空目录（通过后代路径推导为 entry_type='dir'）优先被实际叶子条目覆盖。
    返回按 (compare_prefix, entry_name) 排序的列表。
    """
    records = {}  # key: (compare_prefix, entry_name) -> record

    for row in rows:
        rel_path = row.get("relative_path", "")
        if not rel_path or rel_path == ".":
            continue
        parts = rel_path.split("/")

        if len(parts) <= depth:
            # 路径不够深，无法参与此深度比对
            continue

        compare_prefix = "/".join(parts[:depth]) if depth > 0 else ""
        entry_name = parts[depth]
        is_leaf = (len(parts) == depth + 1)
        key = (compare_prefix, entry_name)

        if key in records:
            existing = records[key]
            if is_leaf and existing["entry_type"] == "dir":
                # 叶子条目覆盖派生目录
                records[key] = make_normalized_record(
                    compare_prefix=compare_prefix,
                    entry_name=entry_name,
                    entry_type=row.get("entry_type", ""),
                    size_bytes=row.get("size_bytes", "0"),
                    modified_utc=row.get("modified_utc", ""),
                    relative_path=rel_path,
                )
            continue

        if is_leaf:
            records[key] = make_normalized_record(
                compare_prefix=compare_prefix,
                entry_name=entry_name,
                entry_type=row.get("entry_type", ""),
                size_bytes=row.get("size_bytes", "0"),
                modified_utc=row.get("modified_utc", ""),
                relative_path=rel_path,
            )
        else:
            # 非空目录：从后代路径推导
            records[key] = make_normalized_record(
                compare_prefix=compare_prefix,
                entry_name=entry_name,
                entry_type="dir",
                size_bytes="",
                modified_utc="",
                relative_path="/".join(parts[:depth + 1]),
            )

    result = sorted(records.values(),
                    key=lambda r: (r["compare_prefix"], r["entry_name"]))
    return result


def _read_tree_csv(path: str) -> Iterator[dict]:
    """逐行读取 tree.csv，返回字典生成器。"""
    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            yield row


def _write_chunk(records: list, tmp_dir: str, chunk_idx: int) -> str:
    """写出一个已排序分块到临时文件，返回文件路径。"""
    fd, path = tempfile.mkstemp(
        prefix=f"chunk_{chunk_idx:06d}_",
        suffix=".csv",
        dir=tmp_dir,
        text=True,
    )
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=NORMALIZED_HEADER)
        writer.writeheader()
        for rec in records:
            writer.writerow(rec)
    return path


def sort_tree_csv(
    tree_path: str,
    depth: int,
    temp_dir: Optional[str] = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> list:
    """对 tree.csv 按比对层级做外部排序，返回临时分块文件路径列表。"""
    temp_dir = temp_dir or tempfile.gettempdir()
    os.makedirs(temp_dir, exist_ok=True)

    chunk_files = []
    buffer = []
    chunk_idx = 0
    row_count = 0

    logger.info("Reading tree.csv: %s", tree_path)

    for row in _read_tree_csv(tree_path):
        buffer.append(row)
        row_count += 1

        if len(buffer) >= chunk_size:
            logger.debug("Processing chunk %d (%d rows)...", chunk_idx, len(buffer))
            normalized = _tree_rows_to_normalized(iter(buffer), depth)
            if normalized:
                path = _write_chunk(normalized, temp_dir, chunk_idx)
                chunk_files.append(path)
            buffer.clear()
            chunk_idx += 1

    # 处理剩余缓冲区
    if buffer:
        logger.debug("Processing final chunk %d (%d rows)...", chunk_idx, len(buffer))
        normalized = _tree_rows_to_normalized(iter(buffer), depth)
        if normalized:
            path = _write_chunk(normalized, temp_dir, chunk_idx)
            chunk_files.append(path)

    logger.info(
        "Sorted %d rows into %d chunks (depth=%d)",
        row_count, len(chunk_files), depth,
    )
    return chunk_files


def merge_chunks(chunk_files: list) -> Iterator[dict]:
    """多路归并已排序分块，产出一致排序的标准化记录流。"""
    if not chunk_files:
        return

    if len(chunk_files) == 1:
        with open(chunk_files[0], "r", newline="", encoding="utf-8") as f:
            yield from csv.DictReader(f)
        return

    # 打开所有分块
    readers = []
    for path in chunk_files:
        f = open(path, "r", newline="", encoding="utf-8")
        reader = csv.DictReader(f)
        try:
            first = next(reader)
        except StopIteration:
            f.close()
            continue
        readers.append((f, reader, first))

    def sort_key(rec):
        return (rec["compare_prefix"], rec["entry_name"])

    # 初始化堆
    heap = [(sort_key(r[2]), i, r[2]) for i, r in enumerate(readers)]
    heapq.heapify(heap)

    while heap:
        _, idx, rec = heapq.heappop(heap)
        yield rec

        f, reader, _ = readers[idx]
        try:
            next_rec = next(reader)
            heapq.heappush(heap, (sort_key(next_rec), idx, next_rec))
        except StopIteration:
            f.close()

    # 清理未消费完的分块
    for f, reader, _ in readers:
        try:
            f.close()
        except Exception:
            pass


def cleanup_chunks(chunk_files: list):
    """删除临时分块文件。"""
    for path in chunk_files:
        try:
            os.remove(path)
        except OSError:
            pass
