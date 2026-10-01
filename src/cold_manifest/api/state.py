"""API 进程内状态：catalog 连接 + 快照库只读连接 LRU 池（§6.1）。"""

import logging
import sqlite3
from collections import OrderedDict
from pathlib import Path
from typing import Any

from ..catalog import connect_catalog, snapshot_path
from ..db import open_snapshot

# 快照库连接池上限（§6.1：LRU，约 16）
_POOL_SIZE = 16


class AppState:
    """持有 data_root 与两类 SQLite 连接；FastAPI lifespan 内创建、退出时关闭。"""

    def __init__(self, data_root: "str | Path") -> None:
        self.data_root = Path(data_root)
        self._catalog: "sqlite3.Connection | None" = None
        self._snap_pools: "OrderedDict[str, sqlite3.Connection]" = OrderedDict()
        # 被 LRU 挤出的连接不在这里 close：可能仍有在途流式导出引用它，
        # close 会掐断读取（Cannot operate on a closed database）。移入 retired，
        # 交给引用计数/GC 兜底，AppState.close() 统一收尾。
        # 元组带 snapshot_id，供 evict_snapshot 按快照定向关闭。
        self._retired: "list[tuple[str, sqlite3.Connection]]" = []

    @property
    def catalog(self) -> sqlite3.Connection:
        """catalog 连接（懒创建；API 只读使用）。"""
        if self._catalog is None:
            self._catalog = connect_catalog(self.data_root, check_same_thread=False)
        return self._catalog

    def snapshot_db(self, snapshot_id: str) -> sqlite3.Connection:
        """按 snapshot_id 取快照库只读连接（mode=ro，不用 immutable），LRU 缓存。

        不用 immutable=1：封库后快照仍会被就地写（hash / build-fts /
        build-stats），immutable 声明的"文件永不变化"不成立，写入后旧连接
        读到错乱页会报假 malformed（见 db.open_snapshot docstring）。
        因此写快照库的任务必须配合 evict_snapshot 逐出池连接。
        snapshot_id 不存在或库文件缺失时抛 LookupError。
        """
        conn = self._snap_pools.get(snapshot_id)
        if conn is not None:
            self._snap_pools.move_to_end(snapshot_id)
            return conn
        path = snapshot_path(self.data_root, snapshot_id)
        if not path.is_file():
            raise LookupError(snapshot_id)
        conn = open_snapshot(path, check_same_thread=False)
        # 大库聚合（stats 单趟 GROUP BY）全内存进行：不落临时盘、用 mmap 省一次拷贝
        conn.execute("PRAGMA mmap_size=1073741824")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-262144")  # 256 MiB 页缓存
        self._snap_pools[snapshot_id] = conn
        if len(self._snap_pools) > _POOL_SIZE:
            evicted_id, evicted = self._snap_pools.popitem(last=False)
            self._retired.append((evicted_id, evicted))
        return conn

    def evict_snapshot(self, snapshot_id: str) -> int:
        """关闭某快照在池内与 retired 里的所有连接（删除前释放 Windows 文件句柄）。

        返回实际关闭的连接数；快照不在池中返回 0，不报错。
        """
        closed = 0
        conn = self._snap_pools.pop(snapshot_id, None)
        if conn is not None:
            try:
                conn.close()
                closed += 1
            except sqlite3.Error:
                pass
        still_retired: "list[tuple[str, sqlite3.Connection]]" = []
        for sid, c in self._retired:
            if sid != snapshot_id:
                still_retired.append((sid, c))
                continue
            try:
                c.close()
                closed += 1
            except sqlite3.Error:
                pass
        self._retired = still_retired
        return closed

    def close(self) -> None:
        for conn in self._snap_pools.values():
            conn.close()
        self._snap_pools.clear()
        for _, conn in self._retired:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self._retired.clear()
        if self._catalog is not None:
            self._catalog.close()
            self._catalog = None


def get_state(request: Any) -> AppState:
    """FastAPI 依赖：从 app.state 取 AppState。"""
    state: AppState = request.app.state.cldm
    return state


def install_snapshot_error_handler(app: Any) -> None:
    """注册 sqlite3.DatabaseError 全局处理器（app 级，覆盖所有 /api 路由）。

    背景：快照库读取失败（尤其 "database disk image is malformed"）过去会
    裸 500 + traceback。malformed 多为历史 immutable 连接在库被就地写
    （hash / build-fts / build-stats）后读到错乱页的"假损坏"——文件没坏，
    重启服务即恢复。这里转成可读中文响应；原文进服务端日志。
    """
    from fastapi.responses import JSONResponse

    log = logging.getLogger(__name__)

    @app.exception_handler(sqlite3.DatabaseError)
    async def _snapshot_db_error(request: Any, exc: sqlite3.DatabaseError) -> Any:
        log.error("快照库读取失败（%s %s）：%s",
                  getattr(request, "method", "?"),
                  getattr(getattr(request, "url", None), "path", "?"),
                  exc, exc_info=exc)
        msg = str(exc).lower()
        if "malformed" in msg or "not a database" in msg or "encrypted" in msg:
            return JSONResponse(
                {"detail": "快照库读取失败，可能被并发写入影响：请重启服务后重试；"
                           "仍失败可用 `cldm integrity-check <快照ID>` 自查，"
                           "必要时重新采集"},
                status_code=409)
        return JSONResponse({"detail": f"快照库读取失败：{exc}"}, status_code=500)
