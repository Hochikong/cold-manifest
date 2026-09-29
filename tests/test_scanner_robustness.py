"""扫描器健壮性：目录级 OSError 不中断采集 + one_filesystem 挂载点剪枝。"""

from __future__ import annotations

import os
from types import SimpleNamespace

from cold_manifest.scanner import scan_tree
from tests.test_scanner import MemWriter


def _make_tree(root):
    (root / "a.txt").write_text("hello")
    (root / "locked").mkdir()          # scandir 将对该目录抛 PermissionError
    (root / "locked" / "secret.txt").write_text("s")
    (root / "sub").mkdir()             # st_dev 将被伪造为不同设备
    (root / "sub" / "b.txt").write_text("x" * 10)
    (root / "tail.txt").write_text("tail")


class _FakeEntry:
    """包装真实 DirEntry；stat() 返回可注入 st_dev 的替身。"""

    def __init__(self, real: os.DirEntry, dev: int) -> None:
        self._real = real
        self._dev = dev

    @property
    def name(self) -> str:
        return self._real.name

    @property
    def path(self) -> str:
        return self._real.path

    def is_symlink(self) -> bool:
        return self._real.is_symlink()

    def stat(self, *, follow_symlinks: bool = True):
        st = self._real.stat(follow_symlinks=follow_symlinks)
        # scan_tree 只用这些字段；构造带替换 st_dev 的替身
        return SimpleNamespace(
            st_mode=st.st_mode, st_dev=self._dev, st_ino=st.st_ino,
            st_size=st.st_size, st_mtime_ns=st.st_mtime_ns,
            st_ctime_ns=getattr(st, "st_ctime_ns", None),
            st_blocks=getattr(st, "st_blocks", None),
        )


def test_scandir_permission_error_continues(tmp_path, monkeypatch):
    """os.scandir 对某目录抛 PermissionError → 记 skipped('scan_error') 并继续，
    其余条目完整入库，绝不中断扫描。"""
    _make_tree(tmp_path)
    real_scandir = os.scandir
    locked = str(tmp_path / "locked")

    def fake_scandir(path):
        if str(path) == locked:
            raise PermissionError(13, "Permission denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", fake_scandir)

    w = MemWriter()
    stats = scan_tree(tmp_path, w)

    paths = {e["path"] for e in w.entries}
    assert "locked" in paths            # 目录行已在库（add_entry 先于 push_frame）
    assert "locked/secret.txt" not in paths
    assert "a.txt" in paths and "tail.txt" in paths  # 扫描继续
    assert ("locked", "scan_error", "scan") == w.skipped[0][:3]
    assert "Permission denied" in w.skipped[0][3]
    assert stats.skipped == 1
    assert stats.files == 3  # a.txt、sub/b.txt、tail.txt（locked/secret 缺席）


def test_other_filesystem_prunes_subtree(tmp_path, monkeypatch):
    """子目录 st_dev 与根不同 → 整棵剪枝 + skipped('other_filesystem')。"""
    _make_tree(tmp_path)
    real_scandir = os.scandir
    root_dev = os.stat(tmp_path).st_dev
    sub = str(tmp_path / "sub")

    def fake_scandir(path):
        it = real_scandir(path)
        return iter(
            _FakeEntry(e, root_dev + 1 if str(e.path).startswith(sub) else root_dev)
            for e in it)

    monkeypatch.setattr(os, "scandir", fake_scandir)

    w = MemWriter()
    stats = scan_tree(tmp_path, w)

    paths = {e["path"] for e in w.entries}
    assert "sub" not in paths and "sub/b.txt" not in paths
    assert "a.txt" in paths and "locked" in paths  # 其余不受影响
    assert any(s[0] == "sub" and s[1] == "other_filesystem" for s in w.skipped)
    assert not any(str(s[0]).startswith("sub/") for s in w.skipped)
    assert stats.skipped == 1


def test_cross_filesystems_follows_mount(tmp_path, monkeypatch):
    """one_filesystem=False（cross_filesystems=True 语义）时不剪枝，正常进入子挂载点。"""
    _make_tree(tmp_path)
    real_scandir = os.scandir
    root_dev = os.stat(tmp_path).st_dev
    sub = str(tmp_path / "sub")

    def fake_scandir(path):
        it = real_scandir(path)
        return iter(
            _FakeEntry(e, root_dev + 1 if str(e.path).startswith(sub) else root_dev)
            for e in it)

    monkeypatch.setattr(os, "scandir", fake_scandir)

    w = MemWriter()
    scan_tree(tmp_path, w, one_filesystem=False)

    paths = {e["path"] for e in w.entries}
    assert "sub" in paths and "sub/b.txt" in paths
    assert not any(s[1] == "other_filesystem" for s in w.skipped)
