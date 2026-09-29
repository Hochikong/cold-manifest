"""后台任务队列（P1.0）：单工作线程 FIFO，任务行持久化 catalog.tasks。

设计（冻结契约 §P1.0）：
- submit() 由 API 线程调用：先在 catalog 插一行 status=pending 再入队；
- API 线程与工作线程**共用同一个** catalog 连接（open_catalog 时
  check_same_thread=False + WAL；sqlite3 默认 serialized 模式下单条
  execute/commit 语句级安全，submit 侧再用 _lock 串行化写事务）；
- progress 由 fn 通过回调上报，写 DB 供前端 1s 轮询；
- start() 做进程重启对账：遗留 running 行置 error、遗留 pending 行重新入队；
- shutdown 时置 stop 事件并 join（排队中的任务保持 pending，进程退出不再执行）；
  join 超时且线程仍存活时不关连接（daemon 随进程退出，避免工作线程写库崩溃）。
"""

import base64
import json
import queue
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .db import init_catalog, open_catalog

# 进度回调频率限制：写 DB 不宜过密（3M 行 tree.csv 每 5 万行一次已够）
ProgressCb = Callable[[str, int, int], None]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_task_id() -> str:
    return f"task_{uuid.uuid4().hex[:12]}"


class TaskRunner:
    """单工作线程 FIFO 任务执行器。"""

    def __init__(self, data_root: "str | Path") -> None:
        self.data_root = Path(data_root)
        self._queue: "queue.Queue[str]" = queue.Queue()
        # 工作线程专用连接：API 线程不触碰
        self._conn: "sqlite3.Connection | None" = None
        self._thread: "threading.Thread | None" = None
        self._stop = threading.Event()
        self._lock = threading.Lock()  # submit 侧串行化 catalog 写

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        if self._thread is not None:
            return
        self._conn = open_catalog(self.data_root / "catalog.db", check_same_thread=False)
        init_catalog(self._conn)  # 建/迁移 tasks 表（幂等）
        self._reconcile()
        self._thread = threading.Thread(target=self._run, name="cldm-tasks", daemon=True)
        self._thread.start()

    def _reconcile(self) -> None:
        """进程重启对账（工作线程启动前、无并发，直接写）：

        - 遗留 running 行：进程重启时已被打断 → 置 error（finished_at=now，
          error='进程重启中断'），避免 has_active_for 永久 409；
        - 遗留 pending 行：尚未执行 → 按 created_at 顺序重新入队。
        """
        now = _now()
        self._conn.execute(
            "UPDATE tasks SET status='error', finished_at=?, error='进程重启中断'"
            " WHERE status='running'",
            (now,),
        )
        rows = self._conn.execute(
            "SELECT task_id FROM tasks WHERE status='pending' ORDER BY created_at, task_id"
        ).fetchall()
        self._conn.commit()
        for r in rows:
            self._queue.put(r["task_id"])

    def stop(self, timeout: float = 10.0) -> None:
        """优雅退出：置 stop、唤醒队列、join 工作线程。

        join 成功才关连接；超时后线程仍存活时不关（daemon 随进程退出即可），
        避免工作线程正在写库时连接被关导致崩溃。
        """
        self._stop.set()
        self._queue.put("")  # 哨兵，解除阻塞的 get
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                return  # 不关连接，交给进程退出
            self._thread = None
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------ 提交 / 查询

    def submit(self, kind: str, payload: dict, fn: Callable[[ProgressCb], Any]) -> str:
        """登记任务（catalog.tasks 插行）并入队，返回 task_id。

        fn(progress_cb) 在工作线程执行；返回值经 json 序列化写入 result_json。
        """
        task_id = new_task_id()
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO tasks(task_id, kind, payload_json, status, created_at)"
                " VALUES(?,?,?,?,?)",
                (task_id, kind, json.dumps(payload, ensure_ascii=False), "pending", now),
            )
            self._conn.commit()
        self._queue.put(task_id)
        return task_id

    def get_task(self, task_id: str) -> "dict | None":
        """任务行 → 契约 Task dict；不存在返回 None。可用任意线程调用。"""
        row = self._conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return _task_row_to_dict(row) if row else None

    def list_tasks(self, limit: int, cursor_created_at: "str | None" = None,
                   cursor_id: "str | None" = None,
                   status: "str | None" = None) -> "tuple[list[dict], str | None, bool]":
        """keyset 分页（created_at DESC + task_id DESC）。"""
        where = ["1=1"]
        params: "list[Any]" = []
        if status:
            where.append("status = ?")
            params.append(status)
        if cursor_created_at is not None and cursor_id:
            where.append("(created_at < ? OR (created_at = ? AND task_id < ?))")
            params += [cursor_created_at, cursor_created_at, cursor_id]
        rows = self._conn.execute(
            f"SELECT * FROM tasks WHERE {' AND '.join(where)}"
            " ORDER BY created_at DESC, task_id DESC LIMIT ?",
            [*params, limit + 1],
        ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = None
        if has_more and rows:
            raw = json.dumps({"c": rows[-1]["created_at"], "i": rows[-1]["task_id"]},
                             ensure_ascii=False, separators=(",", ":"))
            next_cursor = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")
        return [_task_row_to_dict(r) for r in rows], next_cursor, has_more

    def has_active_for(self, key: str) -> bool:
        """同目录（payload.snapshot_dir）是否已有 pending/running 任务。"""
        rows = self._conn.execute(
            "SELECT payload_json FROM tasks WHERE status IN ('pending','running')"
        ).fetchall()
        for r in rows:
            try:
                if json.loads(r["payload_json"]).get("snapshot_dir") == key:
                    return True
            except (json.JSONDecodeError, TypeError):
                continue
        return False

    # ------------------------------------------------------------ 工作线程

    def _run(self) -> None:
        conn = self._conn
        assert conn is not None
        while not self._stop.is_set():
            task_id = self._queue.get()
            if not task_id:  # 哨兵
                break
            row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None or row["status"] not in ("pending",):
                continue  # stop 后遗留的排队任务
            conn.execute(
                "UPDATE tasks SET status='running', started_at=?, message='' WHERE task_id=?",
                (_now(), task_id),
            )
            conn.commit()

            def cb(phase: str, done: int, total: int, _tid: str = task_id) -> None:
                progress = round(done / total, 4) if total and total > 0 else None
                conn.execute(
                    "UPDATE tasks SET progress=?, message=? WHERE task_id=?",
                    (progress, f"{phase}:{done}/{total}", _tid),
                )
                conn.commit()

            try:
                result = _invoke(row, cb)
                conn.execute(
                    "UPDATE tasks SET status='done', progress=1.0, finished_at=?, result_json=?"
                    " WHERE task_id=?",
                    (_now(), json.dumps(result, ensure_ascii=False, default=str), task_id),
                )
            except Exception as e:  # noqa: BLE001 — 任务失败入 error 行，不杀线程
                conn.execute(
                    "UPDATE tasks SET status='error', finished_at=?, error=? WHERE task_id=?",
                    (_now(), f"{type(e).__name__}: {e}", task_id),
                )
            conn.commit()


def _invoke(row: sqlite3.Row, cb: ProgressCb) -> Any:
    """从 task 行还原 fn 并执行。fn 由 kind 决定，经 _FN_REGISTRY 注册。"""
    fn = _FN_REGISTRY.get(row["kind"])
    if fn is None:
        raise RuntimeError(f"未知任务类型：{row['kind']}")
    payload = json.loads(row["payload_json"])
    return fn(payload, cb)


# kind → fn(payload, progress_cb)。由各路由模块在导入时注册，避免 tasks.py 依赖业务层。
_FN_REGISTRY: "dict[str, Callable[[dict, ProgressCb], Any]]" = {}


def register_task_fn(kind: str, fn: Callable[[dict, ProgressCb], Any]) -> None:
    _FN_REGISTRY[kind] = fn


def _task_row_to_dict(row: sqlite3.Row) -> dict:
    result = None
    if row["result_json"]:
        try:
            result = json.loads(row["result_json"])
        except json.JSONDecodeError:
            result = None
    try:
        payload = json.loads(row["payload_json"]) if row["payload_json"] else None
    except json.JSONDecodeError:
        payload = None
    return {
        "id": row["task_id"],
        "type": row["kind"],
        "status": row["status"],
        "progress": row["progress"],
        "message": row["message"],
        "payload": payload,
        "result": result,
        "error": row["error"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }
