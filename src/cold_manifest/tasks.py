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
import logging
import queue
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .catalog import rmtree_ro
from .db import init_catalog, open_catalog
from .seal import is_collect_orphan

# 进度回调频率限制：写 DB 不宜过密（3M 行 tree.csv 每 5 万行一次已够）
ProgressCb = Callable[[str, int, int], None]

# 进度写库节流：≥200ms 才落一次盘（74k 条目/s 的 copy/scan 回调不能逐条直写）
_PROGRESS_MIN_INTERVAL_S = 0.2

# 终态集合
_TERMINAL_STATUSES = ("done", "error", "cancelled")

_log = logging.getLogger(__name__)


class TaskDeleteBlocked(Exception):
    """任务处于 pending/running（非终态），禁止删除（路由层转 409）。"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_collect_orphan(ts_dir: Path) -> bool:
    """兼容别名：孤儿判定已抽取到 seal.py（与 collect._sweep_leftovers 同口径）。"""
    return is_collect_orphan(ts_dir)


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
        # 运行中任务的取消事件（task_id → Event）；任务终态后移除
        self._cancel_events: "dict[str, threading.Event]" = {}

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
        self._sweep_orphan_snapshots()

    def _sweep_orphan_snapshots(self) -> None:
        """清扫 data_root 下未 sealed 的快照目录（进程崩溃/取消残留的孤儿）。

        遍历 <data_root>/<volume_id>/<ts>/：
        - snapshot.db 缺失（建库前崩溃的空目录）→ 删除；
        - snapshot.db 存在但未 sealed 且带 collector_version meta（确认是本工具
          采集产生的脏库）→ 删除；无 collector_version 的库（外部/测试数据）不动；
          带 scan_journal 的未封库目录可续采，保留（P1.2b 断点续采口径）。
        下划线开头的顶层目录（_diffs 等内部目录）跳过。只记日志，不抛异常。
        清扫是写操作：短暂持有 data_root 写锁，被其他写者占用时本轮跳过。
        """
        if not self.data_root.is_dir():
            return
        from .lockfile import DataRootLock, LockBusy

        lock = DataRootLock(self.data_root)
        try:
            lock.acquire()
        except LockBusy as e:
            _log.warning("孤儿清扫跳过（data_root 被占用）：%s", e)
            return
        try:
            self._sweep_orphan_snapshots_locked()
        finally:
            lock.release()

    def _sweep_orphan_snapshots_locked(self) -> None:
        if not self.data_root.is_dir():
            return
        try:
            vol_dirs = [d for d in self.data_root.iterdir()
                        if d.is_dir() and not d.name.startswith("_")]
        except OSError as e:
            _log.warning("孤儿清扫：遍历 data_root 失败：%s", e)
            return
        for vol_dir in vol_dirs:
            try:
                ts_dirs = [d for d in vol_dir.iterdir() if d.is_dir()]
            except OSError as e:
                _log.warning("孤儿清扫：遍历 %s 失败：%s", vol_dir, e)
                continue
            for ts_dir in ts_dirs:
                if not _is_collect_orphan(ts_dir):
                    continue
                try:
                    rmtree_ro(ts_dir)
                except OSError as e:
                    _log.warning("孤儿清扫：删除 %s 失败：%s", ts_dir, e)
                else:
                    _log.info("孤儿清扫：删除未封库快照目录 %s", ts_dir)

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

    @contextmanager
    def _read_conn(self):
        """API 线程专用短连接读。

        工作线程持有 self._conn（check_same_thread=False），sqlite3.Connection
        本身并非线程安全——API 线程与之并发 execute 会偶发
        sqlite3.InterfaceError: bad parameter or other API misuse。
        读路径改为每次调用开独立短连接（WAL 下读不阻塞写、写不阻塞读，
        catalog 读都很小，连接开销可忽略），与写线程完全解耦；写路径维持
        记忆 #36 的 _lock 语义不变。runner stop() 后仍可读（catalog.db 在盘）。
        """
        conn = sqlite3.connect(str(self.data_root / "catalog.db"), uri=True)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            yield conn
        finally:
            conn.close()

    def submit(self, kind: str, payload: dict, related_id: "str | None" = None) -> str:
        """登记任务（catalog.tasks 插行）并入队，返回 task_id。

        fn 由 kind 经 _FN_REGISTRY 注册，工作线程按 kind 还原执行；
        返回值经 json 序列化写入 result_json。
        related_id：关联 ID（collect batch → batch_id）。
        """
        task_id = new_task_id()
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO tasks(task_id, kind, payload_json, status, created_at, related_id)"
                " VALUES(?,?,?,?,?,?)",
                (task_id, kind, json.dumps(payload, ensure_ascii=False), "pending", now,
                 related_id),
            )
            self._conn.commit()
        self._queue.put(task_id)
        return task_id

    def submit_dedup(self, kind: str, payload: dict, field: str,
                     related_id: "str | None" = None) -> "str | None":
        """带去重的提交：锁内完成 check+insert（消除提交去重竞态）。

        同 kind+field 值已有 pending/running 任务 → 返回 None（不插行）；
        否则插行入队，返回 task_id。
        """
        key = payload.get(field)
        with self._lock:
            if self._has_active_for_locked(key, kind=kind, field=field):
                return None
            task_id = new_task_id()
            self._conn.execute(
                "INSERT INTO tasks(task_id, kind, payload_json, status, created_at, related_id)"
                " VALUES(?,?,?,?,?,?)",
                (task_id, kind, json.dumps(payload, ensure_ascii=False), "pending", _now(),
                 related_id),
            )
            self._conn.commit()
        self._queue.put(task_id)
        return task_id

    def _has_active_for_locked(self, key, *, kind: "str | None", field: str) -> bool:
        """has_active_for 的锁内版本（调用方须已持有 _lock）。"""
        sql = "SELECT kind, payload_json FROM tasks WHERE status IN ('pending','running')"
        params: "list[Any]" = []
        if kind is not None:
            sql += " AND kind=?"
            params.append(kind)
        for r in self._conn.execute(sql, params).fetchall():
            try:
                if json.loads(r["payload_json"]).get(field) == key:
                    return True
            except (json.JSONDecodeError, TypeError):
                continue
        return False

    def get_task(self, task_id: str) -> "dict | None":
        """任务行 → 契约 Task dict；不存在返回 None。可用任意线程调用（短连接读）。"""
        with self._read_conn() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return _task_row_to_dict(row) if row else None

    def list_tasks(self, limit: int, cursor_created_at: "str | None" = None,
                   cursor_id: "str | None" = None,
                   status: "str | None" = None,
                   related_id: "str | None" = None) -> "tuple[list[dict], str | None, bool]":
        """keyset 分页（created_at DESC + task_id DESC）。related_id 过滤批次子任务。"""
        where = ["1=1"]
        params: "list[Any]" = []
        if status:
            where.append("status = ?")
            params.append(status)
        if related_id:
            where.append("related_id = ?")
            params.append(related_id)
        if cursor_created_at is not None and cursor_id:
            where.append("(created_at < ? OR (created_at = ? AND task_id < ?))")
            params += [cursor_created_at, cursor_created_at, cursor_id]
        rows = []
        with self._read_conn() as conn:
            rows = conn.execute(
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

    def has_active_for(self, key: str, *, kind: "str | None" = None,
                       field: str = "snapshot_dir") -> bool:
        """同 dedupe 键是否已有 pending/running 任务。

        kind：限定任务类型（import/collect 各自 dedupe）；field：payload 内
        作为去重键的字段（import=snapshot_dir，collect=path）。
        """
        sql = "SELECT kind, payload_json FROM tasks WHERE status IN ('pending','running')"
        params: "list[Any]" = []
        if kind is not None:
            sql += " AND kind=?"
            params.append(kind)
        with self._read_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        for r in rows:
            try:
                if json.loads(r["payload_json"]).get(field) == key:
                    return True
            except (json.JSONDecodeError, TypeError):
                continue
        return False

    # ------------------------------------------------------------ 批次（P1.2a）

    def create_batch_row(self, batch_id: str, disk_id: str, root: str,
                         planned: "list[dict]") -> None:
        """登记采集批次行（catalog.batches；status=running，终态由
        refresh_batch_status 按子任务回写）。planned 为卷目标摘要列表。"""
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO batches(batch_id, disk_id, started_at, status,"
                " planned_volumes_json) VALUES(?,?,?,?,?)",
                (batch_id, disk_id, now, "running",
                 json.dumps({"root": root, "planned": planned}, ensure_ascii=False)),
            )
            self._conn.commit()

    def batch_child_statuses(self, batch_id: str) -> "list[str]":
        """批次全部子任务状态（按 created_at, task_id 稳定序；短连接读）。"""
        with self._read_conn() as conn:
            rows = conn.execute(
                "SELECT status FROM tasks WHERE related_id=? ORDER BY created_at, task_id",
                (batch_id,),
            ).fetchall()
        return [r["status"] for r in rows]

    def delete_batch_and_tasks(self, batch_id: str) -> bool:
        """删除"刚创建、尚未执行"的批次及其 pending 子任务（提交原子性补救）。

        仅限路由在提交中途失败时清理现场：任一子任务已非 pending
        （已被工作线程认领）或批次行不存在 → 不删，返回 False。"""
        with self._lock:
            batch = self._conn.execute(
                "SELECT 1 FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
            if batch is None:
                return False
            rows = self._conn.execute(
                "SELECT status FROM tasks WHERE related_id=?", (batch_id,)
            ).fetchall()
            if any(r["status"] != "pending" for r in rows):
                return False
            self._conn.execute("DELETE FROM tasks WHERE related_id=?", (batch_id,))
            self._conn.execute("DELETE FROM batches WHERE batch_id=?", (batch_id,))
            self._conn.commit()
        return True

    def delete_task(self, task_id: str) -> bool:
        """删除任务登记行（仅终态任务，锁内查状态后删）。

        边界：只删 catalog.tasks 里的登记行，**不删**任务产出的快照/对比/文件
        ——快照有自己的删除入口（cldm delete / DELETE /api/snapshots/{id}），
        这里不越权级联。
        批次子任务（related_id 指向 batch）允许删除：删掉后
        GET /api/batches/{id} 的 summary/tasks 自然按剩余子任务行重算，
        这是可接受行为，不加限制。
        KeyError=任务不存在；TaskDeleteBlocked=pending/running 不可删。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row["status"] not in _TERMINAL_STATUSES:
                raise TaskDeleteBlocked("任务正在运行或排队中，请先取消或等它结束")
            self._conn.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
            self._conn.commit()
        return True

    @staticmethod
    def batch_status_from_children(statuses: "list[str]") -> str:
        """批次终态规则（§4.2）：无子任务=planned；有未终态=running；
        全 done=done；有 error/cancelled=partial。"""
        if not statuses:
            return "planned"
        if any(s in ("pending", "running") for s in statuses):
            return "running"
        if all(s == "done" for s in statuses):
            return "done"
        return "partial"

    def get_batch(self, batch_id: str) -> "dict | None":
        """批次 + 子任务摘要；不存在返回 None。终态回写 batches.status。"""
        with self._read_conn() as conn:
            row = conn.execute(
                "SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        if row is None:
            return None
        try:
            planned_doc = json.loads(row["planned_volumes_json"] or "{}")
        except json.JSONDecodeError:
            planned_doc = {}
        tasks, _, _ = self.list_tasks(limit=500, related_id=batch_id)
        statuses = self.batch_child_statuses(batch_id)
        status = self.batch_status_from_children(statuses)
        planned = planned_doc.get("planned") or []
        if planned and len(statuses) < len(planned):
            # 提交是"先建批次行、再逐条插子任务"；极快的工作线程可能先把已落库的
            # 子任务跑完，让批次瞬时看起来已终态。子任务行没齐就一律按 running
            # 报告（也跳过下面的终态回写），避免客户端/测试过早停止轮询。
            status = "running"
        summary = {s: statuses.count(s) for s in
                   ("pending", "running", "done", "error", "cancelled") if s in statuses}
        if status in ("done", "partial") and row["status"] not in ("done", "partial"):
            if self._conn is None:  # runner 已 stop：无写连接，跳过回写（读仍正常）
                _log.debug("批次终态回写跳过（runner 已停止）：%s", batch_id)
            else:
                with self._lock:
                    self._conn.execute(
                        "UPDATE batches SET status=?, finished_at=? WHERE batch_id=?",
                        (status, _now(), batch_id),
                    )
                    self._conn.commit()
        return {
            "batch_id": batch_id,
            "disk_id": row["disk_id"],
            "status": status,
            "root": planned_doc.get("root", ""),
            "planned_volumes": planned_doc.get("planned", []),
            "summary": summary,
            "tasks": tasks,
            "created_at": row["started_at"],
            "finished_at": row["finished_at"],
        }

    # ------------------------------------------------------------ 取消

    def cancel(self, task_id: str) -> str:
        """取消任务：pending 直接置 cancelled；running 置 cancel 事件（引擎协作取消）。

        返回任务当前状态（pending → "cancelled"，running → "cancelling"，
        工作线程随后落终态 cancelled）。
        KeyError=任务不存在；ValueError=已终态（不可取消）。
        """
        with self._read_conn() as conn:
            row = conn.execute(
                "SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        status = row["status"]
        if status in _TERMINAL_STATUSES:
            raise ValueError(f"任务已终态，不可取消：{status}")
        if status == "pending":
            if self._conn is None:  # runner 已 stop：无写连接
                raise RuntimeError("任务执行器已停止，无法取消")
            with self._lock:
                self._conn.execute(
                    "UPDATE tasks SET status='cancelled', finished_at=?, error='任务取消（未执行）'"
                    " WHERE task_id=?",
                    (_now(), task_id),
                )
                self._conn.commit()
            return "cancelled"
        ev = self._cancel_events.get(task_id)
        if ev is not None:
            ev.set()
        # "cancelling" 仅作为 cancel POST 的响应值，不落库：running 行保持
        # status='running'，工作线程随后协作取消并落终态 cancelled
        return "cancelling"

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
            # CAS 认领：仅当仍为 pending 时置 running，消除与 cancel 的竞态
            # （cancel 先把 pending 置 cancelled → rowcount=0 → 跳过，任务不复活）
            with self._lock:  # 与 API 线程写同一连接：写事务全程持锁
                cur = conn.execute(
                    "UPDATE tasks SET status='running', started_at=?, message=''"
                    " WHERE task_id=? AND status='pending'",
                    (_now(), task_id),
                )
                conn.commit()
            if cur.rowcount == 0:
                continue  # 已被取消（或其他线程认领），不执行

            def cb(phase: str, done: int, total: int, _tid: str = task_id) -> None:
                # 节流：≥200ms 落一次盘；末帧（done>=total）恒写，保证阶段收尾可见
                now = time.monotonic()
                final = bool(total) and done >= total
                if not final and (now - last_write[0]) < _PROGRESS_MIN_INTERVAL_S:
                    return
                last_write[0] = now
                progress = round(done / total, 4) if total and total > 0 else None
                with self._lock:  # 写事务全程持锁（与 API 线程互斥）
                    conn.execute(
                        "UPDATE tasks SET progress=?, message=? WHERE task_id=?",
                        (progress, f"{phase}:{done}/{total}", _tid),
                    )
                    conn.commit()

            last_write = [0.0]
            ev = threading.Event()
            self._cancel_events[task_id] = ev
            try:
                result = _invoke(row, cb, ev)
                # 成功恒 done：fn 正常返回即视为完成，不因 cancel_event 置位改判
                # cancelled（cancelled 只走异常分支）
                with self._lock:
                    conn.execute(
                        "UPDATE tasks SET status='done', progress=1.0, finished_at=?, result_json=?"
                        " WHERE task_id=?",
                        (_now(), json.dumps(result, ensure_ascii=False, default=str), task_id),
                    )
                    conn.commit()
            except Exception as e:  # noqa: BLE001 — 任务失败入 error 行，不杀线程
                with self._lock:
                    if ev.is_set():
                        conn.execute(
                            "UPDATE tasks SET status='cancelled', finished_at=?, error=? WHERE task_id=?",
                            (_now(), f"cancelled: {type(e).__name__}: {e}", task_id),
                        )
                    else:
                        conn.execute(
                            "UPDATE tasks SET status='error', finished_at=?, error=? WHERE task_id=?",
                            (_now(), f"{type(e).__name__}: {e}", task_id),
                        )
                    conn.commit()
            finally:
                self._cancel_events.pop(task_id, None)


def _invoke(row: sqlite3.Row, cb: ProgressCb, cancel_event: "threading.Event | None" = None) -> Any:
    """从 task 行还原 fn 并执行。fn 由 kind 决定，经 _FN_REGISTRY 注册。

    fn 签名 fn(payload, progress_cb, cancel_event)；cancel_event 供
    collect 等长任务协作取消，import 等短任务可忽略。
    """
    fn = _FN_REGISTRY.get(row["kind"])
    if fn is None:
        raise RuntimeError(f"未知任务类型：{row['kind']}")
    payload = json.loads(row["payload_json"])
    return fn(payload, cb, cancel_event)


# kind → fn(payload, progress_cb, cancel_event)。由各路由模块在导入时注册，避免 tasks.py 依赖业务层。
_FN_REGISTRY: "dict[str, Callable[..., Any]]" = {}


def register_task_fn(kind: str, fn: Callable[..., Any]) -> None:
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
        "related_id": row["related_id"],
    }
