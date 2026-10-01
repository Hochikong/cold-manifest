"""path_norm 归一规则的唯一定义（采集与 legacy 导入共用）。

规则：`path.casefold()` —— 与 entries.path_norm 的既有语义一致
（diff 的 case_insensitive 配对、hash_cache 的缓存键都按此解释）。
任何写 entries.path_norm 的代码路径（collect 的批量写入、import_legacy、
`cldm fix-pathnorm` 回填）都必须经过这里，避免两套实现漂移。
"""

from __future__ import annotations


def normalize_path(path: str) -> str:
    """返回路径的 casefold 形式（path_norm 列的取值）。"""
    return path.casefold()
