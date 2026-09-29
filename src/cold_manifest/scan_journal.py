"""scan_journal 侧车（§4.3 断点续采）。

``<snapshot_dir>/scan_journal.jsonl``：每完成一个目录（含其整棵子树）追加一行
相对路径。续采时读取已完成集合，跳过这些子树，只重扫未记录部分。

一致性不变量：**journal 已落盘的行 ⇒ 该子树的 entries/skipped 行已 commit**。
实现上 ScanJournal 在把缓冲行写入文件前先回调 ``on_flush``（collect 层传入
``conn.commit``），崩溃最坏情况是"已提交但 journal 未写"→ 续采重扫该子树，
由 INSERT OR IGNORE 幂等吸收；反向（journal 有、库没有）不可能出现。

journal 文件在采集开始前（建库后立刻）创建：任何进行中的扫描都可续。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

# 侧车文件名（固定；is_collect_orphan / 续采候选探测均按此名判定）
JOURNAL_NAME = "scan_journal.jsonl"

# 缓冲多少目录或多久（先到者）触发一次落盘（落盘前先 on_flush 提交 DB）
FLUSH_EVERY_DIRS = 500
FLUSH_EVERY_S = 5.0


def journal_path(snapshot_dir: "str | Path") -> Path:
    return Path(snapshot_dir) / JOURNAL_NAME


def read_completed(snapshot_dir: "str | Path") -> "set[str] | None":
    """读取已完成目录集合。侧车不存在 → None（不可续采的信号）。

    末行不完整（进程在写行中途被杀）时丢弃末行：宁多扫一个目录，不冒
    把截断行误当完整子树跳过的险。
    """
    p = journal_path(snapshot_dir)
    try:
        raw = p.read_bytes()
    except OSError:
        return None
    if not raw:
        return set()
    lines = raw.decode("utf-8", errors="replace").split("\n")
    if raw.endswith(b"\n"):
        lines = lines[:-1]  # split 产生的末尾空串
    else:
        lines = lines[:-1]  # 丢弃不完整的末行
    return {ln.strip() for ln in lines if ln.strip()}


class ScanJournal:
    """追加式目录完成日志（带缓冲与定时落盘）。

    on_flush()：落盘前回调（collect 层传 conn.commit），保证不变量
    “journal 行 ⇒ DB 已提交”。record() 由 scan_tree 的 journal_cb 逐目录调用。
    """

    def __init__(self, snapshot_dir: "str | Path",
                 on_flush: "Callable[[], None] | None" = None) -> None:
        self._path = journal_path(snapshot_dir)
        self._fh = self._path.open("a", encoding="utf-8")
        self._on_flush = on_flush
        self._buf: list[str] = []
        self._last_flush = time.monotonic()

    def record(self, rel: str) -> None:
        self._buf.append(rel + "\n")
        if (len(self._buf) >= FLUSH_EVERY_DIRS
                or time.monotonic() - self._last_flush >= FLUSH_EVERY_S):
            self.flush()

    def flush(self) -> None:
        if not self._buf:
            self._last_flush = time.monotonic()
            return
        if self._on_flush is not None:
            self._on_flush()  # 先提交 DB，再写 journal（不变量）
        self._fh.write("".join(self._buf))
        self._fh.flush()
        self._buf.clear()
        self._last_flush = time.monotonic()

    def close(self) -> None:
        if self._fh is None:
            return
        try:
            self.flush()
        finally:
            fh, self._fh = self._fh, None
            fh.close()
