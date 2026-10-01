"""SMART 采集/解析/历史（P4-②）。

职责：
- parse_smart：把 smartctl ``-i -H -A -j`` 的 JSON 文本解析为结构化字段，
  字段缺失一律 None，**绝不抛异常**（坏 JSON 返回 ``{"health": "unavailable"}``）；
- read_smart：调用 smartctl 采集（可配置 CLDM_SMARTCTL / CLDM_SMARTCTL_ARGS），
  默认参数失败或无输出时自动重试 ``-d sat``（USB 桥常见），记录生效的 device_type；
- disk_smart 历史表（schema.CATALOG_DDL）的写入/读取/回填（不改 catalog.py）；
- preflight 依赖检查辅助（check_smartctl / device_for_path）。

SMART 拿不到绝不阻断采集——所有采集侧调用方负责 try/except + warning。
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import file_uri

# parse_smart 输出键（API/测试契约）
SMART_KEYS = (
    "health",            # passed | failed | unavailable
    "temperature_c",
    "power_on_hours",
    "reallocated_ct",
    "pending_ct",
    "start_stop_ct",
    "spin_up_ms",
    "device_type",       # smartctl 报告的设备类型（sat/ata/scsi/...）
    "model",
    "serial",
    "firmware",
)

_TIMEOUT = 30


# ---------------------------------------------------------------- 解析


def _attr_raw(table: Any, attr_id: int) -> int | None:
    """从 ata_smart_attributes.table 取指定 id 属性的 raw.value（缺则 None）。"""
    if not isinstance(table, dict):
        return None
    for entry in table.get("table") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("id") == attr_id:
            raw = entry.get("raw") or {}
            try:
                return int(raw.get("value"))
            except (TypeError, ValueError):
                return None
    return None


def _lo16(v: int | None) -> int | None:
    return None if v is None else v & 0xFFFF


def parse_smart(raw: "str | None") -> dict:
    """smartctl JSON 文本 → 结构化字段。坏 JSON/缺失字段不抛异常。

    - health：smart_status.passed → passed/failed，缺 → unavailable；
    - power_on_hours：优先 power_on_time.hours，回退属性 9 raw 低 16 位；
    - reallocated/pending：属性 5/197 raw 低 16 位；start_stop：属性 4；
    - spin_up_ms：属性 3 raw 低 16 位；
    - device_type：device.type（sat/ata/scsi/...）。
    """
    out: dict = {k: None for k in SMART_KEYS}
    out["health"] = "unavailable"
    if not raw or not isinstance(raw, str):
        return out
    try:
        sj = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return out
    if not isinstance(sj, dict):
        return out

    passed = (sj.get("smart_status") or {}).get("passed") \
        if isinstance(sj.get("smart_status"), dict) else None
    out["health"] = {True: "passed", False: "failed"}.get(passed, "unavailable")

    try:
        out["temperature_c"] = int((sj.get("temperature") or {}).get("current"))
    except (TypeError, ValueError):
        out["temperature_c"] = None

    poh = (sj.get("power_on_time") or {}).get("hours") \
        if isinstance(sj.get("power_on_time"), dict) else None
    try:
        out["power_on_hours"] = int(poh)
    except (TypeError, ValueError):
        out["power_on_hours"] = _lo16(_attr_raw(sj.get("ata_smart_attributes"), 9))

    table = sj.get("ata_smart_attributes")
    out["reallocated_ct"] = _lo16(_attr_raw(table, 5))
    out["pending_ct"] = _lo16(_attr_raw(table, 197))
    out["start_stop_ct"] = _attr_raw(table, 4)
    out["spin_up_ms"] = _lo16(_attr_raw(table, 3))

    dev = sj.get("device") if isinstance(sj.get("device"), dict) else {}
    out["device_type"] = dev.get("type") or None
    out["model"] = sj.get("model_name") or sj.get("device_model") or None
    out["serial"] = (sj.get("serial_number") or "").strip() or None
    out["firmware"] = sj.get("firmware_version") or None
    return out


# ---------------------------------------------------------------- smartctl 调用


def smartctl_exec() -> str:
    """smartctl 可执行路径：CLDM_SMARTCTL 覆盖，默认 ``smartctl``。"""
    return os.environ.get("CLDM_SMARTCTL") or "smartctl"


def extra_args() -> list[str]:
    """CLDM_SMARTCTL_ARGS 透传参数（shlex 拆分；未设置/为空 → []）。"""
    return shlex.split(os.environ.get("CLDM_SMARTCTL_ARGS", ""))


def _run_cmd(cmd: list[str]) -> "subprocess.CompletedProcess | None":
    """跑外部命令；找不到/超时/其他 OSError → None（SMART 永不阻断）。"""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _usable(proc: "subprocess.CompletedProcess | None") -> bool:
    """smartctl 输出可用：有 JSON 输出即可（rc≠0 常见于部分属性缺失）。"""
    return proc is not None and bool(proc.stdout.strip()) \
        and proc.stdout.lstrip().startswith("{")


def read_smart(device: str) -> "dict | None":
    """采集一台盘的 SMART。返回 ``{"raw": stdout, "device_type": 生效类型或 ""}``。

    默认参数失败/无输出 → 自动重试 ``-d sat``（USB 桥常见），
    重试成功则 device_type 记 "sat"；两次都拿不到 → None。
    """
    exe = smartctl_exec()
    extra = extra_args()
    proc = _run_cmd([exe, *extra, "-i", "-H", "-A", "-j", device])
    if _usable(proc):
        return {"raw": proc.stdout, "device_type": ""}
    proc = _run_cmd([exe, *extra, "-d", "sat", "-i", "-H", "-A", "-j", device])
    if _usable(proc):
        return {"raw": proc.stdout, "device_type": "sat"}
    return None


# ---------------------------------------------------------------- 失败原因诊断（live 读取）


def _run_cmd_ex(cmd: list[str]) -> "tuple[subprocess.CompletedProcess | None, str | None]":
    """跑外部命令并保留失败类别：返回 (proc, err)。

    err ∈ {None, "timeout", "not_found", "other"}；proc 为 None 时 err 非 None。
    """
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT), None
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except FileNotFoundError:
        return None, "not_found"
    except (OSError, subprocess.SubprocessError):
        return None, "other"


def _excerpt(text: "str | None", limit: int = 2048) -> str:
    if not text:
        return ""
    return text.strip()[:limit]


_PERMISSION_PAT = re.compile(
    r"permission denied|access is denied|access denied|拒绝访问|"
    r"requires? (admin|administrator|elevation)|管理员|elevated privileges",
    re.IGNORECASE,
)
_NO_SMART_PAT = re.compile(
    r"unknown device type|unable to detect device type|no smart|"
    r"not supported|unsupported|usb bridge|不支持",
    re.IGNORECASE,
)
_NOT_FOUND_PAT = re.compile(
    r"no such file|not found|unable to open device|could not open|"
    r"找不到|无法打开|不存在",
    re.IGNORECASE,
)

# reason → 人话（面向磁盘页/端点的可诊断文案）
REASON_MESSAGES = {
    "permission_denied": (
        "权限不足：读取 SMART 需要管理员权限"
        "（Windows 请以管理员身份运行服务/CLI，Linux 需要 root/sudo）"
    ),
    "no_smart": "设备或连接桥不支持 SMART（USB 桥可能需要指定设备类型 -d sat）",
    "not_found": "设备不存在或 smartctl 不可用",
    "timeout": "读取 SMART 超时（盘可能休眠中或无响应）",
    "other": "读取 SMART 失败（原因未知，详见 stderr 片段）",
}


def _classify_attempts(attempts: list[dict]) -> str:
    """按尝试记录归类失败原因（一次确定，不混合）。"""
    for a in attempts:
        if a.get("error") == "timeout":
            return "timeout"
        if a.get("error") == "not_found":
            return "not_found"
    stderrs = " | ".join(a.get("stderr_excerpt") or "" for a in attempts)
    if _PERMISSION_PAT.search(stderrs):
        return "permission_denied"
    if _NO_SMART_PAT.search(stderrs):
        return "no_smart"
    if _NOT_FOUND_PAT.search(stderrs):
        return "not_found"
    return "other"


def read_smart_verbose(device: str) -> dict:
    """现场读取 SMART 并保留失败原因（诊断用；成功路径与 read_smart 等价）。

    返回：
    - ok / raw / device_type：成功时与 read_smart 同义；
    - reason：permission_denied / no_smart / not_found / timeout / other；
    - message：reason 对应的人话；
    - raw_excerpt：成功为 stdout 片段（≤2KB），失败为最后一次 stderr 片段（≤2KB）；
    - attempts：逐次尝试记录 [{device_type, rc, error, stderr_excerpt}]。
    绝不抛异常。
    """
    exe = smartctl_exec()
    extra = extra_args()
    attempts: list[dict] = []
    for dt, dt_args in (("", []), ("sat", ["-d", "sat"])):
        cmd = [exe, *extra, *dt_args, "-i", "-H", "-A", "-j", device]
        proc, err = _run_cmd_ex(cmd)
        if _usable(proc):
            return {
                "ok": True,
                "raw": proc.stdout,
                "device_type": dt,
                "reason": None,
                "message": None,
                "raw_excerpt": _excerpt(proc.stdout),
                "attempts": attempts,
            }
        attempts.append({
            "device_type": dt or "default",
            "rc": None if proc is None else proc.returncode,
            "error": err,
            "stderr_excerpt": _excerpt(getattr(proc, "stderr", None) or "", 512),
        })
    reason = _classify_attempts(attempts)
    return {
        "ok": False,
        "raw": None,
        "device_type": "",
        "reason": reason,
        "message": REASON_MESSAGES[reason],
        "raw_excerpt": attempts[-1]["stderr_excerpt"][:2048],
        "attempts": attempts,
    }


def check_smartctl() -> "str | None":
    """smartctl 可用性探测（--version）。可用返回解析后的可执行路径，否则 None。"""
    exe = smartctl_exec()
    if os.path.isabs(exe):
        resolved = exe if (os.path.isfile(exe) and os.access(exe, os.X_OK)) else None
    else:
        resolved = shutil.which(exe)
    if resolved is None:
        return None
    proc = _run_cmd([resolved, "--version"])
    if proc is None or not (proc.returncode == 0 or proc.stdout.strip()):
        return None
    return resolved


def device_for_path(path: "str | Path", disk_index: "int | None" = None) -> "str | None":
    """定位 path 所在物理盘设备。

    Linux：/proc/mounts 找挂载点 → 分区设备名去掉分区号（sda1→/dev/sda，
    nvme0n1p1→/dev/nvme0n1）；Windows：优先 ``\\\\.\\PhysicalDrive<N>``
    （盘号由调用方从 PowerShell 磁盘序号解析传入，参照 probe/windows.py；
    盘符形态 E: 对 USB 桥接盘常无效），拿不到盘号再退回盘符。
    定位失败抛 ProbeError（调用方转 warning）。
    """
    if sys.platform == "win32":
        if isinstance(disk_index, int):
            return f"\\\\.\\PhysicalDrive{disk_index}"
        letter = str(Path(path).resolve())[:2].rstrip(":")
        if len(letter) == 1 and letter.isalpha():
            return f"{letter}:"
        return None
    from .probe.linux import find_mount_point

    _mp, src = find_mount_point(Path(path))
    base = os.path.basename(src)
    # 分区号后缀：sda1→sda；nvme0n1p1→nvme0n1；mmcblk0p2→mmcblk0
    name = re.sub(r"(p?\d+)$", "", base)
    if name and name != base:
        return f"/dev/{name}"
    return src


# ---------------------------------------------------------------- disk_smart 历史


def record_smart(conn: sqlite3.Connection, disk_id: str, snapshot_id: str,
                 parsed: dict, *, collected_at: "str | None" = None,
                 raw_json: "str | None" = None) -> None:
    """落一行 disk_smart 历史（幂等：同 (disk_id, snapshot_id) 覆盖）。

    parsed 为 parse_smart 输出；raw_json 为 smartctl 原始 JSON 文本。
    不 commit——由调用方事务统一提交。
    """
    when = collected_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT OR REPLACE INTO disk_smart(disk_id, snapshot_id, collected_at, health,"
        " temperature_c, power_on_hours, reallocated_ct, pending_ct, start_stop_ct,"
        " spin_up_ms, device_type, raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (disk_id, snapshot_id, when, parsed.get("health") or "unavailable",
         parsed.get("temperature_c"), parsed.get("power_on_hours"),
         parsed.get("reallocated_ct"), parsed.get("pending_ct"),
         parsed.get("start_stop_ct"), parsed.get("spin_up_ms"),
         parsed.get("device_type"), raw_json),
    )


def list_smart(conn: sqlite3.Connection, disk_id: str) -> "list[dict]":
    """该盘 SMART 历史序列（按 collected_at 升序）。"""
    rows = conn.execute(
        "SELECT * FROM disk_smart WHERE disk_id=? ORDER BY collected_at",
        (disk_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _read_meta_raw(db_path: Path) -> "str | None":
    """只读打开快照库取 meta.smart_raw_json；库缺失/读失败 → None。"""
    if not db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(file_uri(db_path, immutable=False), uri=True)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='smart_raw_json'").fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return row[0] if row and row[0] else None


def record_missing_smart(conn: sqlite3.Connection, data_root: "str | Path") -> int:
    """回填历史：有 meta.smart_raw_json 但 disk_smart 无行的快照逐个补行（幂等）。

    写库（调用方负责 conn 可写）；返回本次补行数。快照库缺失/无 SMART 跳过。
    """
    from .catalog import snapshot_path

    rows = conn.execute(
        "SELECT s.snapshot_id, s.volume_id, v.disk_id, s.collected_at"
        " FROM snapshots s JOIN volumes v ON v.volume_id = s.volume_id"
    ).fetchall()
    have = {r["snapshot_id"] for r in conn.execute(
        "SELECT snapshot_id FROM disk_smart").fetchall()}
    n = 0
    for r in rows:
        sid = r["snapshot_id"]
        if sid in have:
            continue
        raw = _read_meta_raw(snapshot_path(data_root, sid))
        if raw is None:
            continue
        record_smart(conn, r["disk_id"], sid, parse_smart(raw),
                     collected_at=r["collected_at"], raw_json=raw)
        have.add(sid)
        n += 1
    conn.commit()
    return n
