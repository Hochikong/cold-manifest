"""CSV 结构定义：字段列表、表头、行构建器。"""

# ---------- metadata.csv ----------

METADATA_FIELDS = [
    "metric_name",
    "value",
]

METADATA_HEADER = ["metric_name", "value"]


def make_metadata_row(name: str, value: str) -> dict:
    return {"metric_name": name, "value": str(value)}


# ---------- tree.csv ----------

TREE_FIXED_HEADER = [
    "volume_id",
    "relative_path",
    "entry_name",
    "entry_type",
    "size_bytes",
    "modified_utc",
]


def tree_header(max_depth: int) -> list:
    """返回目录树 CSV 表头，含固定列和 level_1 ... level_N。"""
    levels = [f"level_{i}" for i in range(1, max_depth + 1)]
    return TREE_FIXED_HEADER + levels


def make_tree_row(
    volume_id: str,
    relative_path: str,
    entry_name: str,
    entry_type: str,
    size_bytes: int,
    modified_utc: str,
    levels: list,
) -> dict:
    row = {
        "volume_id": volume_id,
        "relative_path": relative_path,
        "entry_name": entry_name,
        "entry_type": entry_type,
        "size_bytes": str(size_bytes),
        "modified_utc": modified_utc,
    }
    for i, val in enumerate(levels, start=1):
        row[f"level_{i}"] = val
    return row


# ---------- warnings.csv ----------

WARNINGS_HEADER = ["stage", "path", "warning_type", "detail"]


def make_warning_row(stage: str, path: str, warning_type: str,
                     detail: str = "") -> dict:
    return {
        "stage": stage,
        "path": path,
        "warning_type": warning_type,
        "detail": detail,
    }


# ---------- diff.csv ----------

DIFF_HEADER = [
    "prefix_path",
    "entry_name",
    "diff_type",
    "entry_type_a",
    "entry_type_b",
    "size_a",
    "size_b",
    "modified_a",
    "modified_b",
    "volume_id_a",
    "volume_id_b",
]


def make_diff_row(
    prefix_path: str,
    entry_name: str,
    diff_type: str,
    entry_type_a: str = "",
    entry_type_b: str = "",
    size_a: str = "",
    size_b: str = "",
    modified_a: str = "",
    modified_b: str = "",
    volume_id_a: str = "",
    volume_id_b: str = "",
) -> dict:
    return {
        "prefix_path": prefix_path,
        "entry_name": entry_name,
        "diff_type": diff_type,
        "entry_type_a": entry_type_a,
        "entry_type_b": entry_type_b,
        "size_a": size_a,
        "size_b": size_b,
        "modified_a": modified_a,
        "modified_b": modified_b,
        "volume_id_a": volume_id_a,
        "volume_id_b": volume_id_b,
    }


# ---------- 比对标准化记录 ----------

NORMALIZED_HEADER = [
    "compare_prefix",
    "entry_name",
    "entry_type",
    "size_bytes",
    "modified_utc",
    "relative_path",
]


def make_normalized_record(
    compare_prefix: str,
    entry_name: str,
    entry_type: str,
    size_bytes: str,
    modified_utc: str,
    relative_path: str,
) -> dict:
    return {
        "compare_prefix": compare_prefix,
        "entry_name": entry_name,
        "entry_type": entry_type,
        "size_bytes": size_bytes,
        "modified_utc": modified_utc,
        "relative_path": relative_path,
    }
