"""scanner 单元测试：临时目录树 + 内存 EntryWriter。"""

from __future__ import annotations

import os
import sys

import pytest

from cold_manifest.scanner import scan_tree


class MemWriter:
    """内存版 EntryWriter：记录条目与跳过，entry_id 自 1 递增。"""

    def __init__(self) -> None:
        self.entries: list[dict] = []
        self.skipped: list[tuple] = []

    def add_entry(self, **kw) -> int:
        kw.setdefault("error", None)
        kw["entry_id"] = len(self.entries) + 1
        self.entries.append(kw)
        return kw["entry_id"]


    def add_skipped(self, path, warning_type, stage, detail=None):
        self.skipped.append((path, warning_type, stage, detail))


def make_tree(root):
    (root / "a.txt").write_text("hello")
    (root / "dir one").mkdir()
    (root / "dir one" / "b.txt").write_text("x" * 10)
    (root / "中文目录").mkdir()
    (root / "中文目录" / "cjk 文件.txt").write_text("你好")
    (root / "empty_dir").mkdir()
    (root / "nested" / "deep" / "deeper").mkdir(parents=True)
    (root / "nested" / "deep" / "deeper" / "f.bin").write_text("z" * 7)
    (root / "logs").mkdir()
    (root / "logs" / "app.log").write_text("log")
    os.symlink(root / "a.txt", root / "link_to_a")
    if sys.platform != "win32":
        (root / ".hidden.txt").write_text("h")
        (root / ".hdir").mkdir()
        (root / ".hdir" / "x.txt").write_text("hx")


@pytest.fixture
def tree(tmp_path):
    make_tree(tmp_path)
    return tmp_path


def e(w, path):
    return next(x for x in w.entries if x["path"] == path)


def test_root_row(tree):
    w = MemWriter()
    scan_tree(tree, w)
    root = w.entries[0]
    assert root["path"] == "."
    assert root["parent_id"] == 0
    assert root["depth"] == 0
    assert root["type"] == "dir"
    assert root["size_bytes"] is None
    # 每个条目 parent 指向已存在的条目 id（根=1）
    assert all(1 <= x["parent_id"] <= len(w.entries) for x in w.entries[1:])
    assert root["entry_id"] == 1


def test_counts_bytes_depth_reconcile(tree):
    w = MemWriter()
    st = scan_tree(tree, w)
    # 文件：a.txt, b.txt, cjk 文件.txt, f.bin, app.log + .hidden.txt, .hdir/x.txt = 7
    assert st.files == 7
    # 目录：root, dir one, 中文目录, empty_dir, nested, deep, deeper, logs, .hdir = 9
    assert st.dirs == 9
    assert st.symlinks == 1
    assert st.others == 0
    assert st.total_bytes == 5 + 10 + len("你好".encode()) + 7 + 3 + 1 + 2
    assert st.total_allocated >= st.total_bytes
    assert st.max_depth == 4  # nested/deep/deeper/f.bin
    assert st.elapsed_s > 0
    # 目录全量成行（含空目录）
    assert e(w, "empty_dir")["type"] == "dir"
    # 类型/parent/depth/名字
    d = e(w, "dir one/b.txt")
    assert d["parent_id"] == e(w, "dir one")["entry_id"]
    assert d["depth"] == 2
    assert d["name"] == "b.txt"
    assert d["ext"] == ".txt"
    assert d["size_bytes"] == 10
    # CJK 与空格名保真
    assert e(w, "中文目录/cjk 文件.txt")["name"] == "cjk 文件.txt"
    # symlink 跳过且不跟随（link 指向的 a.txt 只出现一次）
    assert ("link_to_a", "symlink_skipped", "scan", None) in w.skipped
    assert sum(1 for x in w.entries if x["name"] == "a.txt") == 1
    assert st.skipped == len(w.skipped) == 1


def test_exclude_globs(tree):
    # ** 剪枝：logs 整棵不出现；bare pattern 命中 basename
    w = MemWriter()
    st = scan_tree(tree, w, exclude_globs=["logs"])
    assert not any(x["path"].startswith("logs") for x in w.entries)
    assert ("logs", "excluded", "scan", None) in w.skipped
    assert st.files == 6

    # **/*.log 段级通配：只跳过文件
    w2 = MemWriter()
    st2 = scan_tree(tree, w2, exclude_globs=["**/*.log"])
    assert not any(x["ext"] == ".log" for x in w2.entries)
    assert ("logs/app.log", "excluded", "scan", None) in w2.skipped
    assert e(w2, "logs")["type"] == "dir"
    assert st2.files == 6

    # 目录 ** 前缀：nested/** 剪掉整棵子树
    w3 = MemWriter()
    st3 = scan_tree(tree, w3, exclude_globs=["nested/**"])
    assert not any(x["path"].startswith("nested/") for x in w3.entries)
    assert st3.files == 6

    # ? 与 [seq]：bare pattern 命中任意层 basename
    w4 = MemWriter()
    st4 = scan_tree(tree, w4, exclude_globs=["?.txt", "中文目录/[cb]*"])
    assert not any(x["path"] == "a.txt" for x in w4.entries)
    assert not any(x["path"] == "dir one/b.txt" for x in w4.entries)
    assert not any(x["path"].startswith("中文目录/") for x in w4.entries)
    assert st4.files == 3  # f.bin, app.log, .hidden.txt


def test_exclude_hidden(tree):
    if sys.platform == "win32":
        pytest.skip("POSIX 点隐藏语义")
    w = MemWriter()
    st = scan_tree(tree, w, exclude_hidden=True)
    assert not any(x["path"].startswith(".h") for x in w.entries)
    assert (".hidden.txt", "hidden", "scan", None) in w.skipped
    # .hidden.txt + .hdir（目录命中即剪枝）+ symlink = 3
    assert st.skipped == len(w.skipped) == 3


def test_progress_callback(tree):
    w = MemWriter()
    calls: list[tuple[int, int]] = []
    scan_tree(tree, w, progress_cb=lambda n, b: calls.append((n, b)))
    assert calls, "至少回调一次"
    n_last, b_last = calls[-1]
    total_entries = len(w.entries) + len(w.skipped)
    assert n_last == total_entries
    assert b_last == 5 + 10 + len("你好".encode()) + 7 + 3 + 1 + 2
    # 单调递增
    assert all(calls[i][0] < calls[i + 1][0] for i in range(len(calls) - 1))


def test_small_tree_frequents_callback(tmp_path):
    # 极小树也应有回调（结尾 flush）
    (tmp_path / "only.txt").write_text("1")
    w = MemWriter()
    calls = []
    scan_tree(tmp_path, w, progress_cb=lambda n, b: calls.append(n))
    assert calls[-1] == 2  # root + only.txt


# ---- one_filesystem / st_dev 平台差异（Windows 真盘验证 P0 修复回归）--------

import stat as _stat_mod
from types import SimpleNamespace

from cold_manifest.scanner import _is_cross_fs


class _FakeStat:
    def __init__(self, dev: int, is_dir: bool) -> None:
        self.st_dev = dev
        self.st_mode = 0o040000 if is_dir else 0o100644
        self.st_mtime_ns = 0
        self.st_size = 0


class _FakeEntry:
    def __init__(self, path: str, name: str, dev: int, is_dir: bool) -> None:
        self.path = path
        self.name = name
        self._dev = dev
        self._is_dir = is_dir

    def is_symlink(self) -> bool:
        return False

    def stat(self, follow_symlinks: bool = False) -> _FakeStat:
        return _FakeStat(self._dev, self._is_dir)


def _build_fake_os(tmp_path, root_dev: int, dev_of):
    """从真实临时树构造假 os.scandir/os.stat：dev_of(abs_path) 决定每项 st_dev。"""
    children_map: dict = {}

    def walk(d):
        entries = []
        for child in sorted(d.iterdir(), key=lambda p: p.name):
            is_dir = child.is_dir()
            entries.append(_FakeEntry(str(child), child.name,
                                      dev_of(str(child)) if not is_dir else dev_of(str(child)),
                                      is_dir))
            if is_dir:
                walk(child)
        children_map[str(d)] = entries

    walk(tmp_path)

    class _FakeOS:
        path = os.path
        fspath = staticmethod(os.fspath)

        @staticmethod
        def stat(p):
            return _FakeStat(root_dev, True)

        @staticmethod
        def scandir(p):
            class _Iter:
                def __init__(self, items):
                    self._it = iter(items)

                def __iter__(self):
                    return self

                def __next__(self):
                    return next(self._it)

                def close(self):
                    pass

            return _Iter(children_map.get(str(p), []))

    return _FakeOS


def test_is_cross_fs_semantics():
    root = SimpleNamespace(st_dev=12345)
    # a) Windows 模拟：子目录非 follow st_dev 恒 0 → 不剪
    assert not _is_cross_fs(SimpleNamespace(st_dev=0), root)
    # 一侧为 0（未知）一律放行
    assert not _is_cross_fs(SimpleNamespace(st_dev=0), SimpleNamespace(st_dev=0))
    # b) POSIX 语义：两侧非 0 且不同 → 剪
    assert _is_cross_fs(SimpleNamespace(st_dev=999), root)
    assert not _is_cross_fs(SimpleNamespace(st_dev=12345), root)


def test_windows_zero_dev_children_not_pruned(tmp_path, monkeypatch):
    # 复现 Windows 真盘场景：root st_dev=12345，所有子项 st_dev=0 → 全部入库
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep").mkdir()
    (tmp_path / "r1.txt").write_text("1")
    (tmp_path / "sub" / "s1.txt").write_text("2")
    (tmp_path / "sub" / "deep" / "d1.txt").write_text("3")
    monkeypatch.setattr("cold_manifest.scanner.os",
                        _build_fake_os(tmp_path, 12345, lambda p: 0))
    w = MemWriter()
    st = scan_tree(tmp_path, w)
    paths = {x["path"] for x in w.entries}
    assert {"r1.txt", "sub", "sub/s1.txt", "sub/deep", "sub/deep/d1.txt"} <= paths
    assert st.files == 3
    assert not [s for s in w.skipped if s[1] == "other_filesystem"]


def test_posix_cross_fs_still_pruned(tmp_path, monkeypatch):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "s1.txt").write_text("2")
    (tmp_path / "r1.txt").write_text("1")
    monkeypatch.setattr("cold_manifest.scanner.os",
                        _build_fake_os(tmp_path, 12345,
                                       lambda p: 999 if p.endswith("sub") else 12345))
    w = MemWriter()
    st = scan_tree(tmp_path, w)
    paths = {x["path"] for x in w.entries}
    assert "r1.txt" in paths
    assert "sub" not in paths and "sub/s1.txt" not in paths
    assert ("sub", "other_filesystem", "scan", None) in w.skipped
    assert st.files == 1


def test_cross_filesystems_flag_disables_prune(tmp_path, monkeypatch):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "s1.txt").write_text("2")
    monkeypatch.setattr("cold_manifest.scanner.os",
                        _build_fake_os(tmp_path, 12345,
                                       lambda p: 999 if p.endswith("sub") else 12345))
    w = MemWriter()
    scan_tree(tmp_path, w, one_filesystem=False)
    paths = {x["path"] for x in w.entries}
    assert "sub" in paths and "sub/s1.txt" in paths
    assert not [s for s in w.skipped if s[1] == "other_filesystem"]
