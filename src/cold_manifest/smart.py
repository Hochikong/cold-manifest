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

# ATA 属性 ID → 中文名（属性表显示用）。未知 ID 回退英文原名。
# 用词以"一眼看懂"为准；未列出的少见属性显示 smartctl 原始英文名。
ATA_ATTR_NAMES: dict = {
    1: "原始读取错误率",
    2: "吞吐性能",
    3: "主轴电机起转时间",
    4: "启停次数",
    5: "重分配扇区计数",
    7: "寻道错误率",
    8: "寻道时间性能",
    9: "通电小时数",
    10: "主轴电机重试次数",
    11: "电机校准重试次数",
    12: "通电次数",
    13: "软读取错误率",
    22: "当前磁头飞行高度",      # 仅部分厂商
    170: "可用保留块数",         # 常见于 SSD
    171: "程序失败块数",
    172: "擦除失败块数",
    173: "平均擦除次数",
    174: "意外断电次数",
    177: "磁头加载周期数",
    179: "已用备用磁头数",
    180: "预留磁头数",
    181: "程序失败块数",
    182: "擦除失败块数",
    183: "运行时坏块数",
    184: "端到端错误",
    187: "无法修正的 ECC 错误",
    188: "指令超时次数",
    189: "磁头飞行高度异常",     # 仅部分厂商
    190: "气流温度",
    191: "G 敏感错误率",
    192: "断电收回磁头次数",
    193: "磁头加载/卸载周期",
    194: "温度",
    195: "硬件 ECC 修正计数",
    196: "重分配事件计数",
    197: "当前待映射扇区数",
    198: "离线不可修正扇区数",
    199: "UDMA CRC 错误",
    200: "写入错误率",
    201: "软读取错误率",
    202: "内部数据校验错误",
    203: "运行时坏块跳过次数",
    204: "软 ECC 修正计数",
    205: "热抖动错误率",
    206: "写入错误率（虚拟）",
    207: "读取错误率（虚拟）",
    208: "校验错误率（虚拟）",
    209: "虚拟校验错误率",
    210: "成功验证的写入扇区数",
    211: "飞行抖动错误",
    212: "共振抖动错误",
    220: "盘片偏移错误",
    221: "G 敏感偏移错误",
    222: "磁头加载时间",
    223: "磁头卸载时间",
    224: "负载均衡磨损",
    225: "主机写入次数",
    226: "累计工作载荷时间",
    227: "扭矩放大次数",
    228: "断电收回周期",
    230: "磁头振幅",
    231: "剩余寿命（SSD）",
    232: "预留块剩余数",
    233: "磨损均衡计数",
    234: "预留块磨损",
    235: "上电磨损",
    240: "磁头飞行小时",
    241: "累计写入量",
    242: "累计读取量",
    243: "累计写入量（低 32 位）",
    244: "累计读取量（低 32 位）",
    250: "读取错误重试率",
    251: "最小磨损",
    252: "最大磨损",
    254: "剩余寿命（自由落体）",
}

# when_failed 英文枚举 → 中文（大小写不敏感匹配；其他值原样返回）
_WHEN_FAILED_ZH = {
    "failing_now": "现在失败",
    "in_the_past": "曾经失败",
    "": "",
}


def _when_failed_zh(v: "str | None") -> "str | None":
    if not isinstance(v, str):
        return None
    z = _WHEN_FAILED_ZH.get(v.strip().lower())
    return z if z is not None else v


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
    # 历史行补齐（旧版采集的 ata_json 没有 name_zh/when_failed_zh）：按属性 ID
    # 现查中文名，让已有快照**不重采**也能显示中文（磁盘页与 HTML 报告均受益）。
    # 复制字典再改，绝不修改调用方传入的对象。
    enriched: list = []
    for a in out["ata_attributes"]:
        if not isinstance(a, dict):
            enriched.append(a)
            continue
        need_name = not str(a.get("name_zh") or "").strip()
        need_failed = not str(a.get("when_failed_zh") or "").strip()
        if not (need_name or need_failed):
            enriched.append(a)
            continue
        row = dict(a)
        if need_name:
            try:
                row["name_zh"] = ATA_ATTR_NAMES.get(int(row.get("id"))) or ""
            except (TypeError, ValueError):
                row["name_zh"] = ""
        if need_failed:
            row["when_failed_zh"] = _when_failed_zh(row.get("when_failed"))
        enriched.append(row)
    out["ata_attributes"] = enriched
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
            wf = e.get("when_failed")
            out.append({
                "id": _int_or_none(e.get("id")),
                "name": e.get("name") or None,
                # 中文名：命中 ATA_ATTR_NAMES 给中文，未命中给 ""（不丢英文原名）
                "name_zh": ATA_ATTR_NAMES.get(_int_or_none(e.get("id")), ""),
                "value": _int_or_none(e.get("value")),
                "worst": _int_or_none(e.get("worst")),
                "thresh": _int_or_none(e.get("thresh")),
                "raw_value": raw_value,
                "raw_string": str(raw_string) if raw_string is not None else None,
                # smartctl 用 "" 表示"从未失败"，保留原文；仅缺失才置 None
                "when_failed": wf if isinstance(wf, str) else None,
                "when_failed_zh": _when_failed_zh(wf),
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


# ---------------------------------------------------------------- 设备身份解析
# 设计（PR-B）：定位不再"按盘号下标"信任 smartctl --scan 行序——同型号同容量
# 多盘时下标映射可能错位，读到另一块盘的 SMART。改为：枚举扫描表全部条目，
# 逐台 `smartctl -i -j` 读身份，与期望序列号/容量/型号打分排序，序列号精确
# 匹配的排最前；下标回退形态显式标 risk="unverified_index_mapping"。


def read_device_identity(device: str, suggested_type: "str | None" = None
                         ) -> "dict | None":
    """读一台设备的身份（serial/model/capacity）。失败/无 JSON → None。

    按 ``smartctl -i -j`` 走类型兜底链（_device_types，语义与 read_smart
    一致），第一次读到 JSON 即解析返回：
    ``{serial, capacity_bytes, model, device_type, raw}``。绝不抛异常。
    """
    for dt in _device_types(suggested_type, windows=_is_windows_device(device)):
        cmd = [smartctl_exec(), *extra_args(), *(["-d", dt] if dt else []),
               "-i", "-j", device]
        try:
            proc = _run_cmd(cmd)
            if proc is None or not (proc.stdout or "").lstrip().startswith("{"):
                continue
            if proc.returncode & _FAIL_MASK:
                continue
            sj = json.loads(proc.stdout)
        except Exception:  # noqa: BLE001 — 身份探测绝不抛
            continue
        if not isinstance(sj, dict):
            continue
        dev = sj.get("device") if isinstance(sj.get("device"), dict) else {}
        try:
            cap = int((sj.get("user_capacity") or {}).get("bytes"))
        except (TypeError, ValueError):
            cap = None
        return {
            "serial": (sj.get("serial_number") or "").strip() or None,
            "capacity_bytes": cap,
            "model": sj.get("model_name") or sj.get("device_model")
            or dev.get("model") or None,
            "device_type": dt,
            "raw": proc.stdout,
        }
    return None


def _fmt_capacity(n: "int | None") -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "未知容量"
    for unit, div in (("TB", 10**12), ("GB", 10**9), ("MB", 10**6)):
        if n >= div:
            return f"{n / div:.1f}{unit}"
    return f"{n}B"


def _score_identity_match(ident: "dict | None",
                          expected_serial: "str | None",
                          expected_capacity_bytes: "int | None",
                          expected_model: "str | None") -> int:
    """身份匹配打分。

    - serial 精确(忽略大小写) 100；
    - serial 不一致：expected 常是 USB 硬盘盒上报的 ID（盒 ID），而 SMART
      读到的是真盘序列号——序列号不同**不**单独判"另一块盘"，还要看容量
      和型号：容量≤5% 且型号一致 → 90；容量≤5% 但型号明确不一致 → 70
      （盒型号与真盘型号常见不同，型号不致死但用于同容量时区分）；容量
      ≤5% 型号不可比（任一方缺失）→ 20（弱匹配）；容量≤20% 且型号一致
      50；仅容量≤20% 20；容量差 >20% 或容量不可比 → 0（容量也对不上才是
      另一块盘）；
    - serial 缺失/占位：容量≤5% 且型号匹配 80；容量≤5% 型号明确不一致
      70；容量≤5% 型号不可比 20；容量≤20% 且型号匹配 50；仅容量≤20% 20；
      否则 0。
    """
    if not ident:
        return 0
    serial = (ident.get("serial") or "").strip()
    cap = ident.get("capacity_bytes")
    exp_cap = expected_capacity_bytes
    cap5 = bool(cap and exp_cap and abs(cap - exp_cap) <= 0.05 * max(cap, exp_cap))
    cap20 = bool(cap and exp_cap and abs(cap - exp_cap) <= 0.20 * max(cap, exp_cap))
    ident_model = str(ident.get("model") or "").strip()
    exp_model = str(expected_model or "").strip()
    model_match = bool(exp_model and ident_model
                       and ident_model.upper() == exp_model.upper())
    model_mismatch = bool(exp_model and ident_model
                          and ident_model.upper() != exp_model.upper())
    if expected_serial and serial:
        if serial.upper() == expected_serial.strip().upper():
            return 100
        # 序列号不一致：容量也明显对不上才判"另一块盘"并剔除
        if cap5:
            if model_match:
                return 90
            if model_mismatch:
                return 70
            return 20  # 型号不可比，只能弱匹配
        if cap20 and model_match:
            return 50
        if cap20:
            return 20
        return 0
    if cap5:
        if model_match:
            return 80
        if model_mismatch:
            return 70
        return 20
    if cap20 and model_match:
        return 50
    if cap20:
        return 20
    return 0


def resolve_smart_device(*, expected_serial: "str | None" = None,
                         expected_capacity_bytes: "int | None" = None,
                         expected_model: "str | None" = None,
                         disk_index: "int | None" = None,
                         letter: "str | None" = None
                         ) -> "tuple[list[dict], dict]":
    """按身份（序列号优先）定位设备：返回 (候选链, scan_info)。

    - CLDM_SMARTCTL_DEVICE 环境覆盖优先（mapped_by="env_override"）；
    - 枚举 scan_devices() 全部条目，逐台 read_device_identity（同一次调用
      内按 device 缓存）并 _score_identity_match 打分，按分数降序排候选；
      有 expected_serial 时，读到不同序列号**且容量差 >20%**（或容量不可
      比）的条目（证明是另一块盘）剔除——序列号不同但容量吻合的保留
      （expected 是盒 ID 的正常场景，SMART 读到的是真盘序列号）；
    - 末尾追加 /dev/sd{disk_index} 回退候选（risk="unverified_index_mapping"）
      与盘符候选；
    - scan_info["mapped_by"] ∈ serial_match | unique_capacity_match |
      capacity_match | weak_match | env_override | fallback；
      unique_capacity_match = top 分数唯一（≥70 容量吻合档）且明显高于第
      二名（至少低一档）→ 身份已验证（盒 ID 场景 / 读不到序列号靠容量+
      型号唯一定位均适用）；capacity_match = 达到容量吻合档但多台并列或
      领先不明显 → 不验证；
      scan_info["identity_ambiguity"] 非空表示多台设备无法区分
      （多台同序列号 / 多台容量型号并列 / 多台弱匹配且都读不到序列号），
      附中文原因与建议；scan_info["identity_reason"] 给出人话解释
      （盒 ID 场景说明 / 未验证原因）。
    """
    env = os.environ.get("CLDM_SMARTCTL_DEVICE")
    if env and env.strip():
        cands: "list[dict]" = [{"device": env.strip(), "type": "",
                                "source": "env"}]
        return cands, {
            "devices": scan_devices(), "candidates": [dict(c) for c in cands],
            "device_used": None, "mapped_by": "env_override",
            "mapped_from_scan": False,
        }

    entries = scan_devices()
    has_expected = bool(expected_serial or expected_capacity_bytes
                        or expected_model)
    ident_cache: "dict[str, dict | None]" = {}
    scored: "list[tuple[int, dict]]" = []
    excluded = 0  # 读到不同序列号被剔除的条数
    for e in entries:
        dev = e["device"]
        if dev not in ident_cache:
            ident_cache[dev] = read_device_identity(dev, e.get("type") or "")
        ident = ident_cache[dev]
        score = _score_identity_match(ident, expected_serial,
                                      expected_capacity_bytes,
                                      expected_model)
        # 读到**不同**序列号且容量也明显对不上（score==0）：证明是另一块
        # 盘，不进候选链；容量吻合（score≥20，含 90 盒 ID 场景）或读不到
        # 序列号（-i 失败/桥占位）保留候选，走弱匹配+歧义检测
        if expected_serial and ident and (ident.get("serial") or "").strip() \
                and score == 0:
            excluded += 1
            continue
        entry: dict = {"device": dev, "type": e.get("type") or "",
                       "source": "scan"}
        if has_expected:
            entry["identity_score"] = score
            if ident and ident.get("serial"):
                entry["identity_serial"] = ident["serial"]
        if score == 0:
            entry.setdefault("risk", "unverified_identity")
        scored.append((score, entry))
    scored.sort(key=lambda t: t[0], reverse=True)

    # 无期望值且给了盘号：保持旧行为——只取扫描表对应下标一条（超出范围
    # 则扫描表整条不取，仅剩回退形态）（下标映射本身未经验证，risk 由
    # 回退候选承载）
    if not has_expected and isinstance(disk_index, int):
        scored = [scored[disk_index]] if 0 <= disk_index < len(scored) else []
    elif not has_expected:
        scored = []  # 旧行为：无盘号时扫描表条目不进候选链（只有盘符等形态）

    cands = [e for _s, e in scored]

    # 唯一匹配判定：top 分数唯一且明显高于第二名（至少低一档 ≥10 分），
    # 且 top 达到容量吻合档（≥70，含 90 读到真盘序列号 / 80 容量+型号 /
    # 70 容量吻合型号不符）。不硬性要求读到序列号——有的盒子连真盘序列号
    # 都读不出，只能靠容量+型号唯一定位。多台并列（同容量同型号）不验证。
    top_s = scored[0][0] if scored else 0
    top_count = sum(1 for s, _e in scored if s == top_s)
    second_s = next((s for s, _e in scored if s < top_s), None)
    unique_match = bool(scored) and top_s >= 70 and top_count == 1 \
        and (second_s is None or second_s <= top_s - 10)

    # "已证明没有目标盘"：扫描表非空但每一条都因序列号不符被剔除。
    # 与"扫描表为空"（无法证明）不同——此时绝不再追加未验证的 sd/letter
    # 回退候选，否则可能对一块已证明不是目标盘的设备发起读取。
    target_not_found = has_expected and excluded > 0 and not scored

    # 歧义检测（多台无法区分）
    ambiguity: "str | None" = None
    if ambiguity is None and top_s >= 70 and top_count > 1:
        ties = [e for s, e in scored if s == top_s]
        ambiguity = (
            f"多台设备（{'、'.join(e['device'] for e in ties)}）"
            "容量/型号均与目标一致，无法确定哪块是目标盘；"
            "建议拔掉其中一块再采，或用 --serial 显式区分")
    if expected_serial:
        hits = [e for _s, e in scored
                if str(e.get("identity_serial") or "").strip().upper()
                == expected_serial.strip().upper()
                and e.get("identity_score") == 100]
        if len(hits) > 1:
            ambiguity = (
                f"多台设备（{'、'.join(e['device'] for e in hits)}）"
                f"均报告相同序列号 {expected_serial}，无法确定哪块是目标盘；"
                f"建议拔掉其中一块再采，或用 --serial 显式区分")
    if ambiguity is None and has_expected and len(scored) >= 2:
        weak_no_serial = [e for s, e in scored
                          if e["source"] == "scan" and 0 < s <= 80
                          and not e.get("identity_serial")]
        if len(weak_no_serial) >= 2:
            ambiguity = (
                f"多台设备（{'、'.join(e['device'] for e in weak_no_serial)}）"
                "均无法读出序列号且型号/容量相同，无法确定哪块是目标盘；"
                "建议拔掉其中一块再采，或用 --serial 显式区分")

    # 回退候选：/dev/sd{disk_index}（下标映射未经验证）+ 盘符
    if not target_not_found:
        if isinstance(disk_index, int) and 0 <= disk_index < 26:
            sd = f"/dev/sd{chr(ord('a') + disk_index)}"
            if all(c["device"] != sd for c in cands):
                cands.append({"device": sd, "type": "", "source": "fallback-sd",
                              "risk": "unverified_index_mapping", "note": (
                                  "扫描映射不可用/未通过身份校验时的回退形态；"
                                  "\\\\.\\PhysicalDriveN 不是 smartctl 认可的"
                                  "设备名，一律不进候选链；下标映射未经验证，"
                                  "同型号同容量多盘可能错位")})
        if letter:
            lt = f"{letter.rstrip(':')}:"
            if all(c["device"] != lt for c in cands):
                cands.append({"device": lt, "type": "", "source": "letter"})

    mapped_by = "fallback"
    if cands and cands[0]["source"] == "scan":
        sc = cands[0].get("identity_score")
        if sc == 100:
            mapped_by = "serial_match"
        elif isinstance(sc, int) and sc >= 70:
            mapped_by = ("unique_capacity_match" if unique_match
                         else "capacity_match")
        elif sc:
            mapped_by = "weak_match"
    top_risk = cands[0].get("risk") if cands else None
    scan_info: dict = {
        "devices": entries,
        "candidates": [{"device": c["device"], "type": c.get("type") or "",
                        "source": c.get("source") or "",
                        **({"note": c["note"]} if c.get("note") else {}),
                        **({"risk": c["risk"]} if c.get("risk") else {}),
                        **({"identity_score": c["identity_score"]}
                           if "identity_score" in c else {})}
                       for c in cands],
        "device_used": None,
        "mapped_from_scan": any(c["source"] == "scan" for c in cands),
        "mapped_by": mapped_by,
    }
    if top_risk:
        scan_info["identity_risk"] = top_risk
    if target_not_found:
        scan_info["identity_risk"] = "target_not_found"
        scan_info["identity_reason"] = (
            "扫描到的设备序列号均与目标盘不符（目标盘可能未接入或未换回"
            "原盒）；已跳过 SMART 读取，以免把别的盘参数记到本卷")
    elif unique_match and expected_serial:
        top_ident = ident_cache.get(cands[0]["device"]) or {}
        tser = (top_ident.get("serial") or "").strip()
        if tser:
            scan_info["identity_reason"] = (
                f"探针读到的是硬盘盒上报的 ID（{expected_serial}），SMART 已按"
                f"容量 {_fmt_capacity(top_ident.get('capacity_bytes'))} 唯一定位"
                f"并读到真盘序列号 {tser}")
        else:
            scan_info["identity_reason"] = (
                f"探针读到的是硬盘盒上报的 ID（{expected_serial}），SMART 未能"
                f"读到设备序列号，已按容量 {_fmt_capacity(top_ident.get('capacity_bytes'))}"
                f"+型号（{top_ident.get('model')}）唯一定位")
    elif not entries:
        scan_info["identity_reason"] = (
            "smartctl 扫描表为空，无法按序列号/容量定位设备；已回退到盘号"
            "映射，身份未验证")
    elif mapped_by == "weak_match" and not cands[0].get("identity_serial"):
        scan_info["identity_reason"] = (
            "SMART 未能读到设备序列号，无法按序列号确认身份；已按容量/型号"
            "弱匹配定位（未验证）")
    if ambiguity:
        scan_info["identity_ambiguity"] = ambiguity
    return cands, scan_info


def smart_device_candidates(disk_index: "int | None" = None,
                            letter: "str | None" = None) -> "list[dict]":
    """候选设备串（兼容层）：内部走 resolve_smart_device（无期望值）。

    完整身份解析见 resolve_smart_device；无期望值时行为与旧版一致：
    CLDM_SMARTCTL_DEVICE 覆盖 → 扫描表对应下标 → /dev/sdN 回退 → 盘符。
    """
    cands, _si = resolve_smart_device(disk_index=disk_index, letter=letter)
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
    if not cands and device is None:
        # 没有任何可用候选（身份校验未通过或未提供）：绝不伪造空设备
        # （对 "" 发起调用只会得到 device_used=""、无 attempts、无原因）
        scan = _scan_info([])
        return {
            "ok": False,
            "raw": None,
            "device": "",
            "device_candidates": [],
            "device_type": "",
            "exit_status": None,
            "reason": "not_found",
            "message": "没有可用的设备候选（身份校验未通过或未提供）；"
                       "请确认目标盘已接入，或用 CLDM_SMARTCTL_DEVICE 指定设备串",
            "raw_excerpt": "",
            "attempts": [],
            "scan_info": scan,
        }
    if not cands:
        cands = [{"device": device or "", "type": suggested_type or "",
                  "source": ""}]
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
