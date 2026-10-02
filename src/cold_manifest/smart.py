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

# SSD 专属字段的 API 契约键（前端并行开发中，契约冻结；缺项 null/[]）
SSD_CONTRACT_KEYS = (
    "life_left_pct",         # 寿命剩余 0-100
    "percentage_used",       # NVMe 寿命已用 %
    "available_spare_pct",   # 备用空间 %
    "written_bytes",         # 累计写入字节
    "read_bytes",            # 累计读取字节
    "written_tb",            # 累计写入（TB，人类可读）
    "read_tb",
    "media_errors",
    "unsafe_shutdowns",
    "power_cycles",
    "controller_busy_minutes",
    "temp_sensors",          # [int]
    "source",                # 'nvme' | 'ata'
)

# 关键 HDD 指标 + 身份细节的 API 契约键（冻结；缺项 null/false）
ATA_CONTRACT_KEYS = (
    # 关键 HDD 指标（ATA 属性 ID 见 _ATA_METRIC_ATTRS）
    "power_cycle_count",         # attr 12
    "load_cycle_count",          # attr 193
    "udma_crc_errors",           # attr 199（接口 CRC 错误）
    "raw_read_error_rate",       # attr 1
    "seek_error_rate",           # attr 7
    "spin_retry_count",          # attr 10
    "power_off_retract_count",   # attr 192
    "airflow_temperature_c",     # attr 190
    "head_flying_hours",         # attr 240
    # 身份/链路细节
    "interface_speed_current",   # interface_speed.current.string
    "interface_speed_max",
    "sata_version",              # sata_version.string
    "ata_version",
    "trim",                      # trim.supported（bool）
    "zoned",                     # zoned_device（name/dict → str）
    "model_family",
    "rotation_rate",             # 转速 rpm（SSD 为 0）
    "form_factor",               # form_factor.name（如 2.5 inches）
)

# 关键指标 → ATA 属性 ID（raw 优先 raw.string，可解析整数）
_ATA_METRIC_ATTRS = {
    "power_cycle_count": 12,
    "load_cycle_count": 193,
    "udma_crc_errors": 199,
    "raw_read_error_rate": 1,
    "seek_error_rate": 7,
    "spin_retry_count": 10,
    "power_off_retract_count": 192,
    "airflow_temperature_c": 190,
    "head_flying_hours": 240,
}

# disk_smart 新增列（ata_json + 上述扁平键；幂等迁移见 db._migrate_catalog）
ATA_DB_COLUMNS = (
    ("ata_json", "TEXT"),
    ("power_cycle_count", "INTEGER"),
    ("load_cycle_count", "INTEGER"),
    ("udma_crc_errors", "INTEGER"),
    ("raw_read_error_rate", "INTEGER"),
    ("seek_error_rate", "INTEGER"),
    ("spin_retry_count", "INTEGER"),
    ("power_off_retract_count", "INTEGER"),
    ("airflow_temperature_c", "INTEGER"),
    ("head_flying_hours", "INTEGER"),
    ("interface_speed_current", "TEXT"),
    ("interface_speed_max", "TEXT"),
    ("sata_version", "TEXT"),
    ("ata_version", "TEXT"),
    ("trim", "INTEGER"),
    ("zoned", "TEXT"),
    ("model_family", "TEXT"),
    ("rotation_rate", "INTEGER"),
    ("form_factor", "TEXT"),
)

# NVMe data unit = 1000 × 512 字节（smartmontools 对 data_units_* 的定义）
_NVME_UNIT_BYTES = 1000 * 512

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


def _int_or_none(v: Any) -> "int | None":
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _clamp_life(v: "int | None") -> "int | None":
    """寿命 0-100 归一；越界/None → None。"""
    if v is None or not 0 <= v <= 100:
        return None
    return v


def _ata_attr_entry(sj: dict, attr_id: int) -> "dict | None":
    table = sj.get("ata_smart_attributes")
    if not isinstance(table, dict):
        return None
    for entry in table.get("table") or []:
        if isinstance(entry, dict) and entry.get("id") == attr_id:
            return entry
    return None


def _ata_attr_raw_int(sj: dict, attr_id: int) -> "int | None":
    """属性 raw 值：优先 raw.string（48-bit 多字十进制串，smartctl 已拼好），
    回退 raw.value；解析不了 → None。"""
    entry = _ata_attr_entry(sj, attr_id)
    if entry is None:
        return None
    raw = entry.get("raw") or {}
    for key in ("string", "value"):
        v = _int_or_none(str(raw.get(key)).strip() if raw.get(key) is not None else None)
        if v is not None:
            return v
    return None


def _lba_to_bytes(v: "int | None") -> "int | None":
    """Total_LBAs_Read/Written → 字节（×512）。注意部分盘报的是 32MiB 单位，
    smartmontools 未在 JSON 里标注单位，无法可靠区分——按 LBA 换算是
    最常见口径，解析不了就 None，绝不猜。"""
    return None if v is None else v * 512


def _parse_ssd_nvme(sj: dict) -> "dict | None":
    """nvme_smart_health_information_log → SSD 子字典；无该段 → None。"""
    nvme = sj.get("nvme_smart_health_information_log")
    if not isinstance(nvme, dict):
        return None
    out: dict = {"source": "nvme"}
    pct_used = _int_or_none(nvme.get("percentage_used"))
    if pct_used is not None:
        out["percentage_used"] = pct_used
        out["life_left_pct"] = _clamp_life(100 - pct_used)
    for src, dst in (("available_spare", "available_spare_pct"),):
        v = _int_or_none(nvme.get(src))
        if v is not None:
            out[dst] = v
    out["available_spare_threshold"] = _int_or_none(
        nvme.get("available_spare_threshold"))
    for src, dst in (("data_units_read", "read_bytes"),
                     ("data_units_written", "written_bytes")):
        units = _int_or_none(nvme.get(src))
        if units is not None:
            out[dst] = units * _NVME_UNIT_BYTES
            out["read_tb" if dst == "read_bytes" else "written_tb"] = \
                round(out[dst] / 1e12, 2)
    for key in ("host_reads", "host_writes", "media_errors",
                "num_err_log_entries", "unsafe_shutdowns", "power_cycles"):
        out[key] = _int_or_none(nvme.get(key))
    out["controller_busy_minutes"] = _int_or_none(nvme.get("controller_busy_time"))
    out["warning_temp_time"] = _int_or_none(nvme.get("warning_temp_time"))
    out["critical_comp_time"] = _int_or_none(nvme.get("critical_comp_time"))
    sensors = nvme.get("temperature_sensors")
    out["temp_sensors"] = [
        t for t in (_int_or_none(s) for s in sensors or []) if t is not None
    ] if isinstance(sensors, list) else []
    return out


# ATA 侧 SSD 寿命属性：SSD_Life_Left(231, 255=未知)、
# Media_Wearout_Indicator(233, 100 起步递减)、Percent_Lifetime_Remain(202)
_LIFE_ATTRS = (231, 233, 202)
# 能证明这是 SSD 的 ATA 属性集合
_SSD_ATTRS = (241, 242, 231, 233, 202, 177)


def _parse_ssd_ata(sj: dict) -> "dict | None":
    """ata_smart_attributes 按 ID 提取 SATA/ATA SSD 字段；无 SSD 属性 → None。"""
    ids = {i: _ata_attr_raw_int(sj, i) for i in (241, 242, 231, 233, 202, 177)}
    if all(v is None for v in ids.values()):
        return None
    out: dict = {"source": "ata"}
    for attr, dst in ((241, "written_bytes"), (242, "read_bytes")):
        b = _lba_to_bytes(ids[attr])
        if b is not None:
            out[dst] = b
            out["written_tb" if dst == "written_bytes" else "read_tb"] = \
                round(b / 1e12, 2)
    for attr in _LIFE_ATTRS:
        life = _clamp_life(ids[attr])
        if life is not None:
            out["life_left_pct"] = life
            break
    if ids[177] is not None:
        out["wear_leveling_count"] = ids[177]
    return out


def parse_ssd(sj: dict) -> dict:
    """smartctl JSON dict → SSD 专属子字典；非 SSD / 读不到 → {}（绝不抛）。"""
    try:
        ssd = _parse_ssd_nvme(sj) or _parse_ssd_ata(sj)
        return ssd or {}
    except Exception:  # noqa: BLE001 — 诊断路径绝不抛
        return {}


def ssd_contract(ssd: "dict | None") -> dict:
    """SSD 子字典 → API 契约字段（冻结键集，缺项 null/[]）。"""
    ssd = ssd or {}
    out: dict = {k: None for k in SSD_CONTRACT_KEYS}
    out["temp_sensors"] = []
    for k in ("life_left_pct", "percentage_used", "available_spare_pct",
              "written_bytes", "read_bytes", "written_tb", "read_tb",
              "media_errors", "unsafe_shutdowns", "power_cycles",
              "controller_busy_minutes"):
        out[k] = ssd.get(k)
    ts = ssd.get("temp_sensors")
    if isinstance(ts, list):
        out["temp_sensors"] = [t for t in ts if isinstance(t, int)]
    out["source"] = ssd.get("source")
    return out


def ata_contract(item: "dict | None") -> dict:
    """SMART 数据 → ATA 契约字段（冻结键集，缺项 null/[]/false）。

    item 可以是 parse_smart 输出（字段已齐）或 disk_smart 历史行
    （ata_attributes 存在 ata_json 列、扁平键为列值）。绝不抛异常。
    """
    item = item or {}
    out: dict = {k: None for k in ATA_CONTRACT_KEYS}
    for k in ATA_CONTRACT_KEYS:
        v = item.get(k)
        out[k] = bool(v) if k == "trim" and v is not None else v
    attrs = item.get("ata_attributes")
    if attrs is None:
        raw = item.get("ata_json")
        if raw:
            try:
                attrs = json.loads(raw)
            except (json.JSONDecodeError, TypeError, ValueError):
                attrs = None
    out["ata_attributes"] = attrs if isinstance(attrs, list) else []
    return out


def _parse_ata_attributes(sj: dict) -> list:
    """ata_smart_attributes.table → 规整列表（每项 id/name/value/worst/thresh/
    raw_value/raw_string/when_failed）；段缺失（NVMe）→ []。绝不抛异常。

    raw_value：优先 raw.string 里可解析的整数（48-bit 多字时 smartctl 已拼好
    十进制串），回退 raw.value；都解析不了 → None（raw_string 保留原文）。
    """
    try:
        table = sj.get("ata_smart_attributes")
        if not isinstance(table, dict):
            return []
        out: list = []
        for e in table.get("table") or []:
            if not isinstance(e, dict):
                continue
            raw = e.get("raw") if isinstance(e.get("raw"), dict) else {}
            raw_string = raw.get("string")
            raw_value = _int_or_none(raw_string) if isinstance(raw_string, str) \
                else None
            if raw_value is None:
                raw_value = _int_or_none(raw.get("value"))
            out.append({
                "id": _int_or_none(e.get("id")),
                "name": e.get("name") or None,
                "value": _int_or_none(e.get("value")),
                "worst": _int_or_none(e.get("worst")),
                "thresh": _int_or_none(e.get("thresh")),
                "raw_value": raw_value,
                "raw_string": str(raw_string) if raw_string is not None else None,
                # smartctl 用 "" 表示"从未失败"，保留原文；仅缺失才置 None
                "when_failed": e.get("when_failed")
                if isinstance(e.get("when_failed"), str) else None,
            })
        return out
    except Exception:  # noqa: BLE001 — 诊断路径绝不抛
        return []


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
    out["ssd"] = {}
    out["ata_attributes"] = []
    for k in ATA_CONTRACT_KEYS:
        out[k] = False if k == "trim" else None
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
        out["temperature_c"] = _ata_attr_raw_int(sj, 194)

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

    # 关键 HDD 指标（raw 优先 raw.string——48-bit 多字时 value 只是低字）
    for key, attr_id in _ATA_METRIC_ATTRS.items():
        out[key] = _ata_attr_raw_int(sj, attr_id)

    # 身份/链路细节
    ispeed = sj.get("interface_speed") if isinstance(
        sj.get("interface_speed"), dict) else {}
    out["interface_speed_current"] = (ispeed.get("current") or {}).get("string") \
        if isinstance(ispeed.get("current"), dict) else None
    out["interface_speed_max"] = (ispeed.get("max") or {}).get("string") \
        if isinstance(ispeed.get("max"), dict) else None
    out["sata_version"] = (sj.get("sata_version") or {}).get("string") \
        if isinstance(sj.get("sata_version"), dict) else sj.get("sata_version")
    out["ata_version"] = (sj.get("ata_version") or {}).get("string") \
        if isinstance(sj.get("ata_version"), dict) else sj.get("ata_version")
    trim = sj.get("trim")
    out["trim"] = bool(trim.get("supported")) if isinstance(trim, dict) else False
    zoned = sj.get("zoned_device")
    out["zoned"] = (zoned.get("name") or zoned.get("id")) \
        if isinstance(zoned, dict) else (zoned or None)
    out["model_family"] = sj.get("model_family") or None
    out["ata_attributes"] = _parse_ata_attributes(sj)

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
    out["ssd"] = parse_ssd(sj)
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
            # --scan-open 对打不开的设备附 open_error（权限/休眠等）——
            # 这些条目映射不可靠，跳过不进候选。
            if d.get("open_error"):
                continue
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
    """smartctl 扫描结果（进程内缓存）：优先 ``--scan-open -j``，回退
    ``--scan -j``、文本 ``--scan``。

    --scan-open 会逐台真实打开设备，能顺带给出可用性与正确的 -d 类型，
    并对打不开的设备附 open_error（这些条目跳过，不进映射表）。
    **不缓存失败**：空结果（smartctl 缺失/刚启动盘未就绪/权限等瞬时失败）
    绝不进缓存——否则一次瞬时失败会把整个服务进程的映射永久污染成空，
    后续全部退化成回退形态（"第一次好、后面坏"）。
    空结果原地重试一次后仍空则直接返回，等下次调用再试。
    """
    global _SCAN_CACHE
    if _SCAN_CACHE is not None:
        return _SCAN_CACHE
    exe = smartctl_exec()
    entries: list[dict] = []
    for _attempt in range(2):  # 空结果重试一次（设备枚举可能有启动时延）
        entries = []
        for args in (["--scan-open", "-j"], ["--scan", "-j"], ["--scan"]):
            proc = _run_cmd([exe, *args])
            if proc is not None:
                entries = parse_scan_output(proc.stdout)
            if entries:
                break
        if entries:
            break
    if entries:
        _SCAN_CACHE = entries
    return entries


def smart_device_candidates(disk_index: "int | None", letter: "str | None" = None
                            ) -> "list[dict]":
    """候选设备串（按优先级，去重保序）：[{device, type, source, note?}]。

    source ∈ env / scan / fallback-sd / letter；note（可选）记录候选
    背景，供 scan_info 展示。**只出 /dev/sdN / 盘符形态**——外部调研
    定案：\\\\.\\PhysicalDriveN 不是 smartctl 认可的设备名（只认
    /dev/sd[a-z]、/dev/pdN、X:），裸传报 EINVAL "Invalid argument"，
    与 -d 无关，因此一律不进候选链。
    """
    env = os.environ.get("CLDM_SMARTCTL_DEVICE")
    if env and env.strip():
        return [{"device": env.strip(), "type": "", "source": "env"}]
    cands: "list[dict]" = []

    def _add(device: str, dtype: str, source: str,
             note: "str | None" = None) -> None:
        if device and all(c["device"] != device for c in cands):
            entry = {"device": device, "type": dtype, "source": source}
            if note:
                entry["note"] = note
            cands.append(entry)

    if isinstance(disk_index, int):
        entries = scan_devices()
        if 0 <= disk_index < len(entries):
            e = entries[disk_index]
            _add(e["device"], e["type"], "scan")
        if 0 <= disk_index < 26:
            sd = f"/dev/sd{chr(ord('a') + disk_index)}"
            _add(sd, "", "fallback-sd", note=(
                "扫描映射不可用时的回退形态；\\\\.\\PhysicalDriveN 不是"
                " smartctl 认可的设备名（裸传报 Invalid argument），"
                "一律不进候选链"))
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


def device_candidates_for_path(path: "str | Path",
                               disk_index: "int | None" = None) -> "list[dict]":
    """path 所在物理盘的**完整候选链**（现场读取用）。

    device_for_path 只回首个候选（旧签名保留，不破坏其它调用点）；
    现场读取必须走本函数/候选链——单设备曾导致 USB 桥盘永远打不开。
    有 disk_index（Windows 盘号）直接走 smart_device_candidates 全链
    （scan 映射 + /dev/sdN + \\\\.\\PhysicalDriveN）；只有盘符时退化为
    盘符形态；Linux 复用 /proc/mounts 定位出单候选。
    定位失败返回 []（由调用方转 404/warning），绝不抛异常。
    """
    if isinstance(disk_index, int):
        return smart_device_candidates(disk_index)
    if sys.platform == "win32":
        letter = str(Path(path).resolve())[:2].rstrip(":")
        if len(letter) == 1 and letter.isalpha():
            return smart_device_candidates(None, letter)
        return []
    try:
        dev = device_for_path(path)
    except Exception:  # noqa: BLE001 — 诊断路径绝不抛
        return []
    return [{"device": dev, "type": "", "source": "linux"}] if dev else []


def device_capacity(device: str) -> "int | None":
    """smartctl -i -j 读 user_capacity.bytes（扫描映射容量软校验用）。

    smartctl --scan 行序与 PhysicalDrive 编号一致只是假设；用设备的实际
    容量与盘的已知容量交叉核对可发现错位映射。读取失败 → None（不校验）。
    """
    proc = _run_cmd([smartctl_exec(), "-i", "-j", device])
    if proc is None or not (proc.stdout or "").lstrip().startswith("{"):
        return None
    try:
        sj = json.loads(proc.stdout)
        return int((sj.get("user_capacity") or {}).get("bytes"))
    except (json.JSONDecodeError, ValueError, TypeError):
        return None


# ---------------------------------------------------------------- 类型兜底链

# 每一步都是一次独立尝试。Linux 顺序：auto → sat → sat,12 → sat,auto →
# 扫描建议 → 桥专用；Windows 顺序（_device_types(windows=True)）：扫描建议 →
# sat → sat,12 → sat,auto → 桥专用 → auto（末尾兜底）——实测 Windows 上
# auto 会把 USB 桥误判成 jmb39x 等走错通路（报 Invalid argument），而用户
# 手工 `-d sat /dev/sdN` 能读出真数据，故 sat 必须先于 auto。
# 注意：jms56x/jmb39x/sntjmicron 是 RAID 盒专用（会向 RAID 卷某 LBA 写，
# 普通单盘有覆写风险），绝不进兜底链；usbjmicron(,p/,x) 只在确认芯片时
# 命中，安全。
_BASE_TYPES = ("", "sat", "sat,12", "sat,auto")
# smartctl 的 VALID ARGUMENTS（smartctl --device-type-help）核对结论：
# usbjmicron 可裸用（,p/,x 变体针对不同 JMicron 芯片）；usbsunplus /
# usbprolific 可裸用。jms56x / jmb39x / sntjmicron 是 RAID 盒专用
# （会向 RAID 卷写 LBA，普通单盘有覆写风险），一律不用。
_BRIDGE_TYPES = ("usbjmicron", "usbjmicron,p", "usbjmicron,x",
                 "usbsunplus", "usbprolific")


def _is_windows_device(device: "str | None") -> bool:
    """设备串是否走 Windows 通路（PhysicalDrive 形态/盘符形态，或本机就是
    Windows）。/dev/sdN 在 Windows 上也是同一通路，但类型链与平台相关：
    Windows 上 sat 先于 auto。"""
    if sys.platform == "win32":
        return True
    dev = device or ""
    if dev.startswith("\\\\.\\"):
        return True
    return len(dev) == 2 and dev[1] == ":" and dev[0].isalpha()


def _device_types(suggested: "str | None" = None,
                  windows: bool = False) -> list[str]:
    """类型兜底链（去重保序）。

    - Linux：auto → sat → sat,12 → 扫描建议 → 桥专用；
    - Windows：扫描建议 → sat → sat,12 → 桥专用 → auto（末尾兜底）。
      auto 在 Windows 上会把 USB 桥误判成 jmb39x 等导致 Invalid argument，
      因此只在最后兜底（仍保留：直连 SATA 控制器时 auto 是正确通路）。
    """
    if windows:
        chain = ([suggested] if suggested else []) \
            + ["sat", "sat,12", "sat,auto", *_BRIDGE_TYPES, ""]
    else:
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

    按类型兜底链逐次尝试（Linux: auto → sat → …；Windows: sat → … → auto
    兜底，见 _device_types），第一次读到数据（exit_status 低 2 位无致命错误
    且有 JSON）即返回；全失败 → None。
    """
    for dt in _device_types(suggested_type, windows=_is_windows_device(device)):
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
    r"requires? (admin|administrator|elevation)|管理员|elevated privileges|"
    # smartctl Windows 实测形态：PhysicalDrive 打开失败 Error=5（拒绝访问）；
    # 非管理员时 smartctl 还会在 stderr 提示 limited functionality
    r"error\s*=?\s*5\b|open failed|missing admin rights|limited functionality",
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
        "读取该盘需要管理员权限：请以管理员身份启动服务"
        "（Windows 上 NVMe 盘不需要，USB 桥接盘/PhysicalDrive 需要）。"
        "可复制排查命令：smartctl -i -H -A -j -d sat /dev/sdN"
    ),
    "device_open": (
        "设备打不开（exit_status bit1）：USB 桥接盘通常要用 /dev/sdN 而不是"
        " \\\\.\\PhysicalDriveN；可设 CLDM_SMARTCTL_DEVICE 直接指定设备串"
        "（改环境变量后需重启服务）再试"
    ),
    "cmdline_error": (
        "smartctl 命令行/参数识别失败（exit_status bit0）："
        "检查 CLDM_SMARTCTL_ARGS 与设备串是否正确（改环境变量后需重启服务）"
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
    # 权限文本先于 exit_status 位掩码：实机 USB 桥形态是 rc bit1(2) + 消息
    # "Open failed, Error=5"——按位掩码会误报 device_open，真相是权限不足。
    texts = " | ".join(
        f"{a.get('stderr_excerpt') or ''} {a.get('stdout_messages') or ''}"
        for a in attempts)
    if _PERMISSION_PAT.search(texts):
        return "permission_denied"
    # exit_status 位掩码：bit1(2)=设备打不开、bit0(1)=命令行错误。
    # 跨多次尝试时 bit1 更接近真实原因（一次参数错常伴随一连串打不开，
    # 只看最后一次会把 bit0 当结论），故先全量找 bit1，找不到才用 bit0。
    has_bit0 = False
    for a in attempts:
        es = a.get("exit_status")
        if isinstance(es, int):
            if es & 2:
                return "device_open"
            if es & 1:
                has_bit0 = True
    if has_bit0:
        return "cmdline_error"
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
    cands: "list[dict]" = []
    if devices:
        for d in devices:
            if isinstance(d, dict) and d.get("device"):
                entry = {"device": str(d["device"]),
                         "type": d.get("type") or "",
                         "source": str(d.get("source") or "")}
                if d.get("note"):
                    entry["note"] = str(d["note"])
                cands.append(entry)
            elif isinstance(d, str):
                cands.append({"device": d, "type": "", "source": ""})
    if not cands and device:
        cands = [{"device": device, "type": suggested_type or "",
                  "source": ""}]
    if not cands:
        cands = [{"device": "", "type": "", "source": ""}]
    scan = _scan_info(cands)
    attempts: list[dict] = []

    def _try(dev: str, sug: "str | None") -> "dict | None":
        """一台设备走完整类型兜底链；成功返回结果 dict，失败记 attempts。"""
        types = device_types if device_types is not None \
            else _device_types(sug, windows=_is_windows_device(dev))
        for dt in types:
            cmd = _build_cmd(dev, dt)
            proc, err = _run_cmd_ex(cmd)
            if _usable(proc):
                scan["device_used"] = dev
                return {
                    "ok": True,
                    "raw": proc.stdout,
                    "device": dev,
                    "device_candidates": [c["device"] for c in cands],
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
        return None

    final: "dict | None" = None
    for c in cands:
        final = _try(c["device"], c["type"] or None)
        if final:
            break
    if final is not None:
        return final
    reason = _classify_attempts(attempts)
    return {
        "ok": False,
        "raw": None,
        "device": "",
        "device_candidates": [c["device"] for c in cands],
        "device_type": "",
        "exit_status": None,
        "reason": reason,
        "message": REASON_MESSAGES[reason],
        "raw_excerpt": _excerpt(attempts[-1]["stderr_excerpt"])
        or _excerpt(attempts[-1]["stdout_messages"]),
        "attempts": attempts,
        "scan_info": scan,
    }


def _scan_info(cands: "list[dict]") -> dict:
    """映射结论：扫描到的设备表 + 候选链 + 是否映射成功。

    候选项带 note 时原样透出（合并/动态追加原因），供磁盘页展示
    "实际尝试了哪几条、哪条被合并/跳过（及原因）"。
    """
    try:
        entries = scan_devices()
    except Exception:  # noqa: BLE001 — 诊断信息永不抛
        entries = []
    scanned = {e["device"] for e in entries}
    cinfo = [{"device": c["device"], "type": c.get("type") or "",
              "source": c.get("source") or "",
              **({"note": c["note"]} if c.get("note") else {})}
             for c in cands]
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
    ssd = parsed.get("ssd") or {}
    ata_attrs = parsed.get("ata_attributes")
    values = [disk_id, snapshot_id, when, parsed.get("health") or "unavailable",
              parsed.get("temperature_c"), parsed.get("power_on_hours"),
              parsed.get("reallocated_ct"), parsed.get("pending_ct"),
              parsed.get("start_stop_ct"), parsed.get("spin_up_ms"),
              parsed.get("device_type"), raw_json,
              ssd.get("life_left_pct"), ssd.get("percentage_used"),
              ssd.get("available_spare_pct"), ssd.get("written_bytes"),
              ssd.get("read_bytes"), ssd.get("media_errors"),
              ssd.get("unsafe_shutdowns"), ssd.get("power_cycles"),
              ssd.get("controller_busy_minutes"),
              json.dumps(ssd, ensure_ascii=False) if ssd else None,
              json.dumps(ata_attrs, ensure_ascii=False)
              if isinstance(ata_attrs, list) and ata_attrs else None]
    trim = parsed.get("trim")
    values += [parsed.get(k) for k in (
        "power_cycle_count", "load_cycle_count", "udma_crc_errors",
        "raw_read_error_rate", "seek_error_rate", "spin_retry_count",
        "power_off_retract_count", "airflow_temperature_c", "head_flying_hours",
        "interface_speed_current", "interface_speed_max", "sata_version",
        "ata_version")]
    values.append(None if trim is None else int(trim))  # bool → 0/1
    values += [parsed.get(k) for k in ("zoned", "model_family", "rotation_rate", "form_factor")]
    conn.execute(
        "INSERT OR REPLACE INTO disk_smart(disk_id, snapshot_id, collected_at, health,"
        " temperature_c, power_on_hours, reallocated_ct, pending_ct, start_stop_ct,"
        " spin_up_ms, device_type, raw_json,"
        " life_left_pct, percentage_used, available_spare_pct, written_bytes,"
        " read_bytes, media_errors, unsafe_shutdowns, power_cycles,"
        " controller_busy_minutes, ssd_json,"
        " ata_json, power_cycle_count, load_cycle_count, udma_crc_errors,"
        " raw_read_error_rate, seek_error_rate, spin_retry_count,"
        " power_off_retract_count, airflow_temperature_c, head_flying_hours,"
        " interface_speed_current, interface_speed_max, sata_version, ata_version,"
        " trim, zoned, model_family, rotation_rate, form_factor)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        values,
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
