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
    "capacity_bytes",    # user_capacity.bytes（Windows CIM 拿不到容量时的补救）
    "rotation_rate",     # 转速（rpm；SSD 为 0）
    "form_factor",       # form_factor.name（如 3.5 inches）
)

_TIMEOUT = 30

# exit_status 位掩码语义（smartctl 文档）：
#   bit0(1)   命令行解析出错           → 失败
#   bit1(2)   设备打不开               → 失败
#   bit2(4)   某些子命令不支持/失败（有 JSON 输出仍算读到数据）
#   bit3(8)   盘报 SMART FAILING       → 成功，健康=failed
#   bit4(16)  预失败属性超阈值         → 成功，健康=warning
#   bit5-7(32/64/128) 历史告警/日志    → 成功，健康=warning
_FAIL_MASK = 3
_HEALTH_FAIL_BIT = 8
_HEALTH_WARN_BITS = 16 | 32 | 64 | 128


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


def parse_smart(raw: "str | None", exit_status: "int | None" = None) -> dict:
    """smartctl JSON 文本 → 结构化字段。坏 JSON/缺失字段不抛异常。

    - health：smart_status.passed → passed/failed；缺失时按 exit_status 位掩码
      推断（bit3=8 → failed；bit4-7 → warning；否则 unavailable）；
    - power_on_hours：优先 power_on_time.hours，回退属性 9 raw 低 16 位；
    - reallocated/pending：属性 5/197 raw 低 16 位；start_stop：属性 4；
    - spin_up_ms：属性 3 raw 低 16 位；
    - device_type：device.type（sat/ata/scsi/...）；
    - model/serial：model_name（回退 device_model / device.model）/ serial_number；
    - capacity_bytes/rotation_rate/form_factor：user_capacity.bytes 等诊断字段。
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
    if passed is True:
        out["health"] = "passed"
    elif passed is False:
        out["health"] = "failed"
    else:
        bits = exit_status or 0
        if bits & _HEALTH_FAIL_BIT:
            out["health"] = "failed"
        elif bits & _HEALTH_WARN_BITS:
            out["health"] = "warning"

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
    out["model"] = sj.get("model_name") or sj.get("device_model") \
        or dev.get("model") or None
    out["serial"] = (sj.get("serial_number") or "").strip() or None
    out["firmware"] = sj.get("firmware_version") or None
    try:
        out["capacity_bytes"] = int((sj.get("user_capacity") or {}).get("bytes"))
    except (TypeError, ValueError):
        out["capacity_bytes"] = None
    try:
        out["rotation_rate"] = int(sj.get("rotation_rate"))
    except (TypeError, ValueError):
        out["rotation_rate"] = None
    ff = sj.get("form_factor")
    out["form_factor"] = ff.get("name") if isinstance(ff, dict) else (ff or None)
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
    """smartctl 输出可用：有 JSON 输出且 exit_status 低 2 位无致命错误。

    exit_status 位掩码：bit0(1)=命令行错误、bit1(2)=设备打不开 → 失败；
    bit3-7（8/16/32/64/128）是健康告警位——有数据就算成功（健康标记由
    parse_smart 按 exit_status 推断），message 里的 warning 不算失败。
    """
    if proc is None or not proc.stdout.strip() \
            or not proc.stdout.lstrip().startswith("{"):
        return False
    return (proc.returncode & _FAIL_MASK) == 0


# ---------------------------------------------------------------- 设备名解析
# Windows USB 桥实测定案：\\.\PhysicalDriveN 常打不开（Invalid argument），而
# smartctl --scan 给出的 /dev/sdN + -d sat 才能读到。因此 Windows 上优先用
# --scan 的映射（scan 输出顺序与 disk_index 一致），拿不到再回退 PhysicalDrive。

_SCAN_CACHE: "list[dict] | None" = None

_SCAN_LINE_PAT = re.compile(r"^(\S+)\s+-d\s+([^\s,#+]+)")


def reset_scan_cache() -> None:
    """清空 --scan 结果缓存（测试/设备热插拔后用）。"""
    global _SCAN_CACHE
    _SCAN_CACHE = None


def _scan_entries_from_json(text: str) -> "list[dict] | None":
    """``--scan -j`` JSON → [{device, type}]；非 JSON / 解析失败 → None。

    实测（smartctl 7.5 Windows，宿主两块 NVMe）：
      {"json_format_version":[1,0], "devices":[{"name":"/dev/sda",
       "info_name":"/dev/sda","type":"nvme","protocol":"NVMe"}, ...]}
    name 优先，info_name 兜底；type 缺失记 ""。
    """
    if not (text or "").lstrip().startswith("{"):
        return None
    try:
        sj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(sj, dict):
        return None
    entries: list[dict] = []
    for d in sj.get("devices") or []:
        if isinstance(d, dict):
            name = d.get("name") or d.get("info_name")
            if name:
                entries.append({"device": str(name),
                                "type": str(d.get("type") or "")})
    return entries


def parse_scan_output(text: str) -> "list[dict]":
    """smartctl --scan 输出（文本或 -j JSON）→ [{device, type}]。

    实测样例（Windows USB 桥）：
      /dev/sdc -d sat # [SAT], ATA device
    JSON 形态见 _scan_entries_from_json（7.5 起支持 `--scan -j`）。
    """
    from_json = _scan_entries_from_json(text)
    if from_json is not None:
        return from_json
    entries: list[dict] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _SCAN_LINE_PAT.match(line)
        if m:
            entries.append({"device": m.group(1), "type": m.group(2)})
    return entries


def scan_devices() -> "list[dict]":
    """smartctl --scan 结果（进程内缓存）：优先 -j JSON，回退文本解析。"""
    global _SCAN_CACHE
    if _SCAN_CACHE is not None:
        return _SCAN_CACHE
    exe = smartctl_exec()
    entries: list[dict] = []
    proc = _run_cmd([exe, "--scan", "-j"])
    if proc is not None:
        entries = parse_scan_output(proc.stdout)
    if not entries:
        proc = _run_cmd([exe, "--scan"])
        if proc is not None:
            entries = parse_scan_output(proc.stdout)
    _SCAN_CACHE = entries
    return entries


def smart_device_candidates(disk_index: "int | None", letter: "str | None" = None
                            ) -> "list[dict]":
    """候选设备串（按优先级，去重保序）：[{device, type, source}]。

    source ∈ env / scan / fallback-sd / fallback-pd / letter。
    --scan 顺序与 PhysicalDrive 编号一致（映射可用时首选）；映射不可用
    （盘不在扫描结果里，USB 桥常见）时同时给出 /dev/sdN 与
    \\\\.\\PhysicalDriveN 两种形态，由调用链依序尝试——单选回退曾导致
    USB 盘永远打不开（PhysicalDrive 形态在桥上常报 Invalid argument）。
    """
    env = os.environ.get("CLDM_SMARTCTL_DEVICE")
    if env and env.strip():
        return [{"device": env.strip(), "type": "", "source": "env"}]
    cands: "list[dict]" = []

    def _add(device: str, dtype: str, source: str) -> None:
        if device and all(c["device"] != device for c in cands):
            cands.append({"device": device, "type": dtype, "source": source})

    if isinstance(disk_index, int):
        entries = scan_devices()
        if 0 <= disk_index < len(entries):
            e = entries[disk_index]
            _add(e["device"], e["type"], "scan")
        if 0 <= disk_index < 26:
            _add(f"/dev/sd{chr(ord('a') + disk_index)}", "", "fallback-sd")
        _add(f"\\\\.\\PhysicalDrive{disk_index}", "", "fallback-pd")
    elif letter:
        _add(f"{letter}:", "", "letter")
    return cands


def smart_device(disk_index: "int | None", letter: "str | None" = None
                 ) -> "tuple[str, str]":
    """定位要采 SMART 的设备串。返回 (device, 扫描建议的 -d 类型或 "")。

    取 smart_device_candidates 首选：CLDM_SMARTCTL_DEVICE 覆盖 → --scan
    第 disk_index 行 → /dev/sdN → \\\\.\\PhysicalDriveN。完整候选链见
    smart_device_candidates。
    """
    cands = smart_device_candidates(disk_index, letter)
    if not cands:
        return "", ""
    return cands[0]["device"], cands[0]["type"]


# ---------------------------------------------------------------- 类型兜底链

# 每一步都是一次独立尝试（auto → sat → sat,12 → 扫描建议 → 桥专用）
_BASE_TYPES = ("", "sat", "sat,12")
_BRIDGE_TYPES = ("usbjmicron", "usbsunplus", "usbprolific", "jms56x")


def _device_types(suggested: "str | None" = None) -> list[str]:
    chain = list(_BASE_TYPES)
    if suggested:
        chain.append(suggested)
    chain.extend(_BRIDGE_TYPES)
    seen: set[str] = set()
    out: list[str] = []
    for t in chain:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _build_cmd(device: str, device_type: str) -> list[str]:
    exe = smartctl_exec()
    extra = extra_args()
    dt_args = ["-d", device_type] if device_type else []
    return [exe, *extra, *dt_args, "-i", "-H", "-A", "-j", device]


def read_smart(device: str, *, suggested_type: "str | None" = None) -> "dict | None":
    """采集一台盘的 SMART。返回 ``{"raw", "device_type", "exit_status"}``。

    按类型兜底链逐次尝试（auto → sat → sat,12 → 扫描建议 → 桥专用），
    第一次读到数据（exit_status 低 2 位无致命错误且有 JSON）即返回；全失败 → None。
    """
    for dt in _device_types(suggested_type):
        cmd = _build_cmd(device, dt)
        proc = _run_cmd(cmd)
        if _usable(proc):
            return {"raw": proc.stdout, "device_type": dt,
                    "exit_status": proc.returncode}
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
    r"not supported|unsupported|usb bridge|不支持|invalid argument",
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
    "device_open": (
        "设备打不开（exit_status bit1）：USB 桥接盘通常要用 /dev/sdN 而不是"
        " \\\\.\\PhysicalDriveN；可设 CLDM_SMARTCTL_DEVICE 直接指定设备串后重试"
    ),
    "cmdline_error": (
        "smartctl 命令行/参数识别失败（exit_status bit0）："
        "检查 CLDM_SMARTCTL_ARGS 与设备串是否正确"
    ),
    "device_type_unknown": (
        "无法识别设备类型（USB 桥常见；已尝试 auto/sat/桥专用参数均失败，"
        "可设 CLDM_SMARTCTL_DEVICE 直接指定设备串）"
    ),
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
    # exit_status 位掩码优先：bit1(2)=设备打不开、bit0(1)=命令行错误
    #（低 2 位致命的尝试本就不会被当成功，rc 在此即真实失败位）
    for a in attempts:
        es = a.get("exit_status")
        if isinstance(es, int):
            if es & 2:
                return "device_open"
            if es & 1:
                return "cmdline_error"
    texts = " | ".join(
        f"{a.get('stderr_excerpt') or ''} {a.get('stdout_messages') or ''}"
        for a in attempts)
    if _PERMISSION_PAT.search(texts):
        return "permission_denied"
    if _NO_SMART_PAT.search(texts):
        return "device_type_unknown"
    if _NOT_FOUND_PAT.search(texts):
        return "not_found"
    return "other"


def _messages_excerpt(proc: "subprocess.CompletedProcess | None",
                      limit: int = 2048) -> str:
    """从失败响应的 stdout JSON 提取人话：messages[].string（-j 出错时
    stdout 仍是 JSON，stderr 往往为空，真正的原因在 messages 里）。"""
    out = (getattr(proc, "stdout", None) or "")
    if not out.lstrip().startswith("{"):
        return ""
    try:
        sj = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return ""
    parts: list[str] = []
    for container in (sj, (sj.get("smartctl") or {}) if isinstance(
            sj.get("smartctl"), dict) else {}):
        for m in (container.get("messages") or []):
            if isinstance(m, dict) and m.get("string"):
                parts.append(str(m["string"]))
    return " | ".join(parts)[:limit]


def read_smart_verbose(device: "str | None" = None, *,
                       suggested_type: "str | None" = None,
                       device_types: "list[str] | None" = None,
                       devices: "list[dict] | None" = None) -> dict:
    """现场读取 SMART 并保留失败原因（诊断用；成功路径与 read_smart 等价）。

    - device：单设备串（与 suggested_type 搭配，向后兼容）；
    - devices：smart_device_candidates 的候选列表 [{device, type, source}]，
      依序尝试（每台设备走完整类型兜底链），扫描映射不可用时两种设备
      形态（/dev/sdN、\\\\.\\PhysicalDriveN）都能试到；
    - ok / raw / device_type / exit_status：成功时与 read_smart 同义；
      另带 device（生效设备串）；
    - reason：permission_denied / device_open / cmdline_error /
      device_type_unknown / not_found / timeout / other；
    - message：reason 对应的人话；
    - raw_excerpt：成功为 stdout 片段（≤2KB），失败为最后一次 stderr 片段（≤2KB）；
    - attempts：失败尝试记录 [{device, device_type, argv, rc, exit_status,
      error, stderr_excerpt, stdout_messages, ok}]（成功的那次由返回字段
      直接承载；stdout_messages 为 -j 失败响应里 messages[].string 的人话）；
    - scan_info：{devices(扫描表), candidates(候选及来源), device_used,
      mapped_from_scan}——"映射结论"，供磁盘页展示。
    device_types 可显式覆盖兜底链（测试用）；绝不抛异常。
    """
    cands: "list[tuple[str, str | None]]" = []
    if devices:
        for d in devices:
            if isinstance(d, dict) and d.get("device"):
                cands.append((str(d["device"]), d.get("type") or None))
            elif isinstance(d, str):
                cands.append((d, None))
    if not cands and device:
        cands = [(device, suggested_type)]
    if not cands:
        cands = [("", None)]
    cand_sources = [d.get("source", "") for d in devices
                    if isinstance(d, dict)] if devices else []
    scan = _scan_info(cands, cand_sources)
    attempts: list[dict] = []
    for dev, sug in cands:
        types = device_types if device_types is not None \
            else _device_types(sug)
        for dt in types:
            cmd = _build_cmd(dev, dt)
            proc, err = _run_cmd_ex(cmd)
            if _usable(proc):
                scan["device_used"] = dev
                return {
                    "ok": True,
                    "raw": proc.stdout,
                    "device": dev,
                    "device_candidates": [c[0] for c in cands],
                    "device_type": dt,
                    "exit_status": proc.returncode,
                    "reason": None,
                    "message": None,
                    "raw_excerpt": _excerpt(proc.stdout),
                    "attempts": attempts,
                    "scan_info": scan,
                }
            attempts.append({
                "device": dev,
                "device_type": dt or "default",
                "argv": " ".join(cmd),
                "rc": None if proc is None else proc.returncode,
                "exit_status": None if proc is None else proc.returncode,
                "error": err,
                "ok": False,
                "stderr_excerpt": _excerpt(getattr(proc, "stderr", None) or "", 512),
                "stdout_messages": _messages_excerpt(proc, 512),
            })
    reason = _classify_attempts(attempts)
    return {
        "ok": False,
        "raw": None,
        "device": "",
        "device_candidates": [c[0] for c in cands],
        "device_type": "",
        "exit_status": None,
        "reason": reason,
        "message": REASON_MESSAGES[reason],
        "raw_excerpt": _excerpt(attempts[-1]["stderr_excerpt"])
        or _excerpt(attempts[-1]["stdout_messages"]),
        "attempts": attempts,
        "scan_info": scan,
    }


def _scan_info(cands: "list[tuple[str, str | None]]",
               sources: "list[str]") -> dict:
    """映射结论：扫描到的设备表 + 候选链 + 是否映射成功。"""
    try:
        entries = scan_devices()
    except Exception:  # noqa: BLE001 — 诊断信息永不抛
        entries = []
    scanned = {e["device"] for e in entries}
    cinfo = []
    for i, (dev, typ) in enumerate(cands):
        src = sources[i] if i < len(sources) else ""
        cinfo.append({"device": dev, "type": typ or "", "source": src})
    return {
        "devices": entries,
        "candidates": cinfo,
        "device_used": None,
        "mapped_from_scan": any(
            c["source"] == "scan" or c["device"] in scanned for c in cinfo),
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
            dev, _sug = smart_device(disk_index)
            return dev
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
