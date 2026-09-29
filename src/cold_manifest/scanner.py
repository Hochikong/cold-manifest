"""采集扫描引擎（升级方案 v0.3 §4.3）。

单遍 ``os.scandir`` 递归，产出统一条目流（§4.1 entries 模型）：
- 根行 entry_id=1、path='.'、parent_id=0、depth=0；子项 path 用 '/' 分隔的相对路径；
- 目录全量成行；符号链接与 Windows 重解析点恒跳过并记账，不跟随；
- exclude_globs 命中目录即整棵剪枝并记 skipped('excluded')；
- stat 失败记 skipped('stat_error')，**不产出条目**——缺 stat 字段的行会在
  后续 dir_rollup / diff 阶段引入 NULL 语义问题，宁可缺席并留痕。

本模块只负责遍历与过滤；建库、批量事务、断点续采由 collect 编排层（另一单元）负责。
"""

from __future__ import annotations

import os
import re
import stat
import sys
import time
from dataclasses import dataclass
from typing import Callable, Protocol

_WIN = sys.platform == "win32"

FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

# progress_cb 每 ~1 万条回调一次
_PROGRESS_INTERVAL = 10_000


@dataclass
class ScanStats:
    files: int = 0
    dirs: int = 0
    symlinks: int = 0
    others: int = 0
    total_bytes: int = 0
    total_allocated: int = 0
    max_depth: int = 0
    skipped: int = 0
    elapsed_s: float = 0.0


class EntryWriter(Protocol):
    def add_entry(
        self,
        *,
        parent_id: int,
        path: str,
        name: str,
        depth: int,
        type: str,
        size_bytes: int | None,
        allocated_bytes: int | None,
        mtime_ns: int | None,
        ctime_ns: int | None,
        btime_ns: int | None,
        attrs: int | None,
        ext: str,
        error: str | None = None,
    ) -> int: ...  # 返回 entry_id

    def add_skipped(
        self, path: str, warning_type: str, stage: str, detail: str | None = None
    ) -> None: ...


def _translate_glob(pat: str) -> re.Pattern[str]:
    """把 exclude-glob 编译为正则（fnmatch 语义 + 段级 ``**``）。

    - ``*`` / ``?`` 不跨 '/'；``**/`` 匹配零个或多个完整段；段内 ``**`` 简化为任意串。
    - ``[seq]`` / ``[!seq]`` 字符类；未闭合的 ``[`` 按字面量处理。
    """
    out: list[str] = ["\\A"]
    i, n = 0, len(pat)
    while i < n:
        c = pat[i]
        if c == "*":
            if pat.startswith("**/", i):
                out.append("(?:[^/]+/)*")
                i += 3
            elif pat.startswith("**", i):
                out.append(".*")
                i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = pat.find("]", i + 1)
            if j < 0:
                out.append(re.escape(c))
                i += 1
            else:
                inner = pat[i + 1 : j]
                if inner.startswith("!"):
                    inner = "^" + inner[1:]
                out.append("[" + inner + "]")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    out.append("\\Z")
    return re.compile("".join(out))


class _ExcludeMatcher:
    """相对路径排除匹配。

    含 '/' 的 pattern 对完整相对路径匹配；不含 '/' 的 pattern 对任意层的
    basename 匹配（gitignore 惯例，`*.log` 能命中任意目录下的日志）。
    """

    def __init__(self, globs: list[str]) -> None:
        self._with_slash = [(_translate_glob(g), g) for g in globs if "/" in g]
        self._bare = [(_translate_glob(g), g) for g in globs if "/" not in g]

    def hit(self, rel_path: str, name: str) -> bool:
        return any(rx.match(rel_path) or rx.match(name) for rx, _ in self._bare) or any(
            rx.match(rel_path) for rx, _ in self._with_slash
        )


def scan_tree(
    root: str | Path,
    writer: EntryWriter,
    *,
    exclude_globs: list[str] | None = None,
    exclude_hidden: bool = False,
    include_system: bool = True,
    progress_cb: Callable[[int, int], None] | None = None,
) -> ScanStats:
    stats = ScanStats()
    t0 = time.monotonic()
    root = os.fspath(root)
    matcher = _ExcludeMatcher(list(exclude_globs or []))

    root_st = os.stat(root)
    root_name = os.path.basename(os.path.abspath(root)) or root
    root_id = writer.add_entry(
        parent_id=0,
        path=".",
        name=root_name,
        depth=0,
        type="dir",
        size_bytes=None,
        allocated_bytes=None,
        mtime_ns=root_st.st_mtime_ns,
        ctime_ns=getattr(root_st, "st_ctime_ns", None),
        btime_ns=getattr(root_st, "st_birthtime_ns", None),
        attrs=getattr(root_st, "st_file_attributes", None) or root_st.st_mode,
        ext="",
    )
    stats.dirs += 1

    since_progress = 0
    done = 1  # 根行

    def flush_progress() -> None:
        nonlocal since_progress
        if progress_cb is not None and since_progress >= _PROGRESS_INTERVAL:
            progress_cb(done, stats.total_bytes)
            since_progress = 0

    # (parent_id, 相对前缀, 子项 depth, 绝对路径)
    stack: list[tuple[int, str, int, str]] = [(root_id, "", 1, root)]
    while stack:
        parent_id, prefix, depth, dpath = stack.pop()
        try:
            it = os.scandir(dpath)
        except OSError as exc:
            rel = prefix or "."
            writer.add_skipped(rel, "scandir_error", "scan", str(exc))
            stats.skipped += 1
            continue
        with it:
            for entry in it:
                name = entry.name
                rel = name if not prefix else prefix + "/" + name
                done += 1
                since_progress += 1

                # 名字级过滤在 stat 之前做，省一次系统调用；目录命中即整棵剪枝
                if matcher.hit(rel, name):
                    writer.add_skipped(rel, "excluded", "scan")
                    stats.skipped += 1
                    continue
                if exclude_hidden and _is_hidden(name, entry):
                    writer.add_skipped(rel, "hidden", "scan")
                    stats.skipped += 1
                    continue

                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    # 选项：不产出条目。缺 stat 的半行会让 rollup/diff 的
                    # NULL 传播复杂化；skipped 里已有 path + detail 可对账。
                    writer.add_skipped(rel, "stat_error", "scan", str(exc))
                    stats.skipped += 1
                    continue

                if entry.is_symlink():
                    # 符号链接（含 Windows symlink/junction 的 POSIX 视角）恒跳过，不跟随
                    writer.add_skipped(rel, "symlink_skipped", "scan")
                    stats.symlinks += 1
                    stats.skipped += 1
                    continue
                if _WIN and getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT:
                    # Windows 重解析点（junction/mount point 等）恒跳过，不跟随
                    writer.add_skipped(rel, "reparse_point", "scan")
                    stats.skipped += 1
                    continue

                if not prefix and name == "_coldmanifest" and stat.S_ISDIR(st.st_mode):
                    # 工具自身元数据目录（盘上副本 <scan_root>/_coldmanifest）恒剪枝，
                    # 不进快照 → 不进 diff 命名空间
                    writer.add_skipped(rel, "excluded", "scan")
                    stats.skipped += 1
                    continue

                if not include_system and _is_system(st):
                    writer.add_skipped(rel, "system", "scan")
                    stats.skipped += 1
                    continue

                is_dir = stat.S_ISDIR(st.st_mode)
                mtime_ns = st.st_mtime_ns
                ctime_ns = getattr(st, "st_ctime_ns", None)
                btime_ns = getattr(st, "st_birthtime_ns", None)
                attrs = getattr(st, "st_file_attributes", None)
                if attrs is None:
                    attrs = st.st_mode
                if is_dir:
                    size_bytes: int | None = None
                    allocated: int | None = None
                    ext = ""
                else:
                    size_bytes = st.st_size
                    # st_blocks*512 为簇对齐真实占用；不可用（如 Windows）回退逻辑大小
                    allocated = getattr(st, "st_blocks", None)
                    allocated = allocated * 512 if allocated is not None else size_bytes
                    ext = os.path.splitext(name)[1].lower()

                if is_dir:
                    etype = "dir"
                elif stat.S_ISREG(st.st_mode):
                    etype = "file"
                else:
                    etype = "other"

                eid = writer.add_entry(
                    parent_id=parent_id,
                    path=rel,
                    name=name,
                    depth=depth,
                    type=etype,
                    size_bytes=size_bytes,
                    allocated_bytes=allocated,
                    mtime_ns=mtime_ns,
                    ctime_ns=ctime_ns,
                    btime_ns=btime_ns,
                    attrs=attrs,
                    ext=ext,
                )

                if depth > stats.max_depth:
                    stats.max_depth = depth
                if etype == "dir":
                    stats.dirs += 1
                    stack.append((eid, rel, depth + 1, entry.path))
                elif etype == "file":
                    stats.files += 1
                    stats.total_bytes += size_bytes or 0
                    stats.total_allocated += allocated or 0
                else:
                    stats.others += 1
                flush_progress()

    if progress_cb is not None and since_progress > 0:
        progress_cb(done, stats.total_bytes)
    stats.elapsed_s = time.monotonic() - t0
    return stats


def _is_hidden(name: str, entry: os.DirEntry) -> bool:
    if _WIN:
        try:
            return bool(entry.stat(follow_symlinks=False).st_file_attributes & FILE_ATTRIBUTE_HIDDEN)
        except OSError:
            return False
    return name.startswith(".")


def _is_system(st: os.stat_result) -> bool:
    """仅 Windows 有意义：FILE_ATTRIBUTE_SYSTEM 位。"""
    return bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_SYSTEM)
