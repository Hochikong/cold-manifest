"""data_root 跨进程写锁（§4.3）：同一 data_root 同时只允许一个写者。

写者（CLI collect、服务端 collect 任务、启动孤儿清扫）在动作期间持有
``<data_root>/.cldm.lock`` 的 OS 级排他锁：POSIX 用 fcntl.flock、Windows 用
msvcrt.locking。进程崩溃后锁由 OS 自动释放，无陈锁问题。

- serve 本身不长期持锁（查询只读），只在执行采集/清扫任务时短暂持有；
- 拿不到锁：CLI → LockBusy → 退出码 2；服务端任务 → 任务 error；
- 环境变量 ``CLDM_SKIP_LOCK=1`` 可禁用（测试用）；
- 锁文件内容 {pid, host, started_at} 仅用于诊断"谁占着锁"。
"""

from __future__ import annotations

import json
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

_WIN = sys.platform == "win32"

if _WIN:
    import msvcrt
else:
    import fcntl

LOCK_NAME = ".cldm.lock"


class LockBusy(Exception):
    """data_root 已被其他写进程占用。"""


def _skip_lock() -> bool:
    return os.environ.get("CLDM_SKIP_LOCK") == "1"


class DataRootLock:
    """非阻塞排他锁；with 语法或显式 acquire/release。"""

    def __init__(self, data_root: "str | Path") -> None:
        self.path = Path(data_root) / LOCK_NAME
        self._fh = None

    def acquire(self) -> None:
        """拿锁；被占用时抛 LockBusy（附持有者诊断信息）。"""
        if _skip_lock():
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # "a+"：读已有内容做诊断；锁失败时不破坏持有者写的诊断信息
        fh = open(self.path, "a+", encoding="utf-8")
        holder = ""
        try:
            fh.seek(0)
            holder = fh.read(512).strip()
        except OSError:
            pass
        try:
            if _WIN:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            fh.close()
            who = holder or "（锁文件无诊断信息）"
            raise LockBusy(
                f"data_root 被占用：{self.path.parent} 正被另一写进程使用（{who}）。"
                f"请等待对方采集/清扫完成后重试。") from e
        try:
            fh.seek(0)
            fh.truncate(0)
            fh.write(json.dumps({
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }, ensure_ascii=False))
            fh.flush()
        except OSError:
            pass  # 诊断信息写失败不影响锁本身
        self._fh = fh

    def release(self) -> None:
        if self._fh is None:
            return
        fh, self._fh = self._fh, None
        try:
            if _WIN:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass  # 关闭句柄也会释放锁
        finally:
            fh.close()

    def __enter__(self) -> "DataRootLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
