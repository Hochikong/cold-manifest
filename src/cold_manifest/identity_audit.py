"""身份审计（PR-C）：对既有采集数据做"串盘"追溯检测（纯只读，绝不改库/文件）。

背景：同型号同容量双盘时，设备定位可能错位、把另一块盘的 SMART 与序列号
写进快照（甚至污染 volume_id）。本模块不重采，只用快照库里现存的序列号
证据做体检：

- ``meta.disk_serial`` / ``meta.physical_serial`` / ``meta.serial_source``
- ``meta.smart_raw_json``（smartctl 报告里的 serial_number）
- ``meta.smart_scan_info_json``（扫描映射结论：candidates / mapped_from_scan）

告警分级：
- intra_snapshot_serial_drift（high）：同一快照内 smart 侧证据自相矛盾
  （physical_serial vs smart_raw 不一致；或 serial_source=smartctl 时
  disk_serial 与 smart_raw 不一致；或 disk_serial 与 physical_serial 不一致
  且无法用"USB 桥上报值 vs 真盘序列号"解释）；
- inter_snapshot_serial_volatility（high）：同一 volume_id 的多次快照之间
  **同一字段**的真盘序列号证据发生变化（逐字段比较，不混字段）；
- high_fallback_ratio（medium）：某盘过半快照走了未验证回退；
- fallback_minority（low）：有快照走未验证回退但未过半（信息性提示）。

USB 桥正常形态不告警：disk_serial 是桥/外壳上报的 ID，而
physical_serial == smart_raw.serial_number（smartctl 读到的真盘序列号）
自洽——这只说明 disk_serial 来自桥而非盘体，写入 notes 不计告警。

容错：快照库缺失/损坏/未封库、meta 缺键、JSON 坏一律计入 warnings，不抛异常。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

__all__ = ["audit_identities", "disk_identity_status", "IdentityAuditError"]


class IdentityAuditError(Exception):
    """数据根/catalog 无法审计（CLI 退出码 2 / API 400）。"""


# ---------------------------------------------------------------- 只读连接


def _connect_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    return column in cols


# ---------------------------------------------------------------- 证据提取


def _norm_serial(v: Any) -> str:
    return str(v or "").strip().upper()


def _raw_serial_from_smart(raw_text: str) -> str:
    """smart_raw_json（smartctl -j 输出）里的 serial_number；解析失败返回 ''。"""
    try:
        data = json.loads(raw_text)
        if isinstance(data, dict):
            return _norm_serial(data.get("serial_number"))
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    try:
        from . import smart

        return _norm_serial(smart.parse_smart(raw_text).get("serial_number"))
    except Exception:  # noqa: BLE001 — 证据提取永不抛
        return ""


def _json_dict(raw: Any) -> dict:
    try:
        data = json.loads(raw) if raw else None
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}


def _snapshot_fallback_flags(meta: dict) -> "list[str]":
    """该快照是否走了未验证回退，返回命中的旗标名列表。"""
    flags: "list[str]" = []
    scan_info = _json_dict(meta.get("smart_scan_info_json"))
    if scan_info:
        cands = scan_info.get("candidates")
        if isinstance(cands, list) and any(
                isinstance(c, dict) and c.get("source") == "fallback-sd"
                for c in cands):
            flags.append("fallback_sd_candidate")
        if scan_info.get("mapped_from_scan") is False:
            flags.append("not_mapped_from_scan")
    if (meta.get("serial_source") or "").strip() == "volume_serial_fallback":
        flags.append("volume_serial_fallback")
    return flags


def _iter_snapshot_meta(catalog: sqlite3.Connection, data_root: Path,
                        disk_id: str, warnings: "list[dict]",
                        status_override: "dict | None" = None,
                        ) -> "list[tuple[str, str, dict | None]]":
    """某盘全部快照的 (snapshot_id, volume_id, meta)。meta 读取失败给 None。"""
    rows = catalog.execute(
        """
        SELECT s.snapshot_id, s.volume_id FROM snapshots s
        JOIN volumes v ON v.volume_id = s.volume_id
        WHERE v.disk_id = ? ORDER BY s.volume_id, s.collected_at
        """,
        (disk_id,),
    ).fetchall()
    out: "list[tuple[str, str, dict | None]]" = []
    for r in rows:
        sid = r["snapshot_id"]
        db_path = data_root / sid.split("/")[0] / sid.split("/")[1] / "snapshot.db"
        meta: "dict | None" = None
        try:
            if db_path.is_file():
                conn = _connect_ro(db_path)
                try:
                    meta = {row["key"]: row["value"]
                            for row in conn.execute("SELECT key, value FROM meta")}
                finally:
                    conn.close()
            else:
                warnings.append({"snapshot_id": sid,
                                 "issue": "snapshot_db_missing",
                                 "detail": f"快照库文件缺失：{db_path}"})
        except sqlite3.Error as e:
            warnings.append({"snapshot_id": sid, "issue": "snapshot_db_error",
                             "detail": str(e)})
        if meta is None and not any(w.get("snapshot_id") == sid for w in warnings):
            warnings.append({"snapshot_id": sid, "issue": "snapshot_db_missing",
                             "detail": f"快照库文件缺失：{db_path}"})
        if isinstance(meta, dict) and (meta.get("status") or "") != "sealed":
            warnings.append({"snapshot_id": sid, "issue": "not_sealed",
                             "detail": f"快照未封库（meta.status={meta.get('status')!r}），"
                                       "证据不完整，跳过"})
            meta = None
        out.append((sid, r["volume_id"], meta))
    return out


# ---------------------------------------------------------------- 身份状态派生


def disk_identity_status(catalog: sqlite3.Connection, data_root: Path,
                         disk_id: str) -> "tuple[str, str]":
    """派生某盘的 (identity_status, identity_status_reason)。

    - conflict：catalog 身份冲突字段非空或 identity_verified==0（列存在时）；
    - verified：identity_verified==1；
    - unverified：任一快照走了 fallback / 未验证映射 / 卷序列号兜底；
    - unknown：其余（本轮尚无 identity_verified 的写入方，旧数据多为此态）。
    """
    has_conflict = _has_column(catalog, "disks", "identity_conflict_json")
    has_verified = _has_column(catalog, "disks", "identity_verified")
    row = catalog.execute("SELECT * FROM disks WHERE disk_id=?", (disk_id,)).fetchone()
    if row is None:
        return "unknown", "catalog 中无该盘记录"
    if has_conflict:
        conflict_raw = (row["identity_conflict_json"] or "").strip()
        if conflict_raw:
            fields = list(_json_dict(conflict_raw).keys()) or [conflict_raw[:80]]
            return "conflict", f"身份字段冲突：{'、'.join(str(f) for f in fields)}"
    if has_verified:
        verified = row["identity_verified"]
        if verified == 0:
            return "conflict", "最近一次身份校验未通过（identity_verified=0）"
        if verified == 1:
            return "verified", "最近一次身份校验通过（identity_verified=1）"
    warnings: "list[dict]" = []
    for _sid, _vid, meta in _iter_snapshot_meta(
            catalog, data_root, disk_id, warnings):
        if meta and _snapshot_fallback_flags(meta):
            return ("unverified",
                    "存在走未验证回退（fallback 候选/扫描未命中/卷序列号兜底）的快照，"
                    "设备映射未经核实")
    return "unknown", "无足够证据判定该盘身份（建议重新采集时开启身份校验）"


# ---------------------------------------------------------------- 审计主入口


def audit_identities(data_root: "str | Path") -> dict:
    """对数据根下全部快照做身份体检（只读）。返回：

    ``{"alerts": [...], "affected_disk_ids": [...],
       "summary": {"high": n, "medium": n, "low": n},
       "warnings": [...], "notes": [...]}``
    """
    data_root = Path(data_root)
    cat_path = data_root / "catalog.db"
    if not cat_path.is_file():
        raise IdentityAuditError(f"catalog 不存在：{cat_path}")
    try:
        catalog = _connect_ro(cat_path)
    except sqlite3.Error as e:
        raise IdentityAuditError(f"catalog 无法打开：{e}") from e

    alerts: "list[dict]" = []
    warnings: "list[dict]" = []
    notes: "list[dict]" = []
    try:
        disks = [r["disk_id"] for r in catalog.execute(
            "SELECT disk_id FROM disks ORDER BY disk_id")]
        for disk_id in disks:
            snaps = _iter_snapshot_meta(catalog, data_root, disk_id,
                                        warnings)
            _audit_disk(disk_id, snaps, alerts, notes)
    finally:
        catalog.close()

    affected = sorted({a["disk_id"] for a in alerts if a.get("disk_id")})
    summary = {
        "high": sum(1 for a in alerts if a.get("severity") == "high"),
        "medium": sum(1 for a in alerts if a.get("severity") == "medium"),
        "low": sum(1 for a in alerts if a.get("severity") == "low"),
    }
    return {"alerts": alerts, "affected_disk_ids": affected,
            "summary": summary, "warnings": warnings, "notes": notes}


def _snapshot_serials(meta: dict) -> "dict[str, str]":
    """提取单快照逐字段序列号证据：disk_serial / physical_serial / smart_raw。"""
    raw_serial = _raw_serial_from_smart(meta.get("smart_raw_json") or "")
    return {k: v for k, v in (
        ("disk_serial", _norm_serial(meta.get("disk_serial"))),
        ("physical_serial", _norm_serial(meta.get("physical_serial"))),
        ("smart_raw", raw_serial),
    ) if v}


def _intra_drift_alert(disk_id: str, sid: str, vid: str, meta: dict,
                       serials: "dict[str, str]") -> "dict | None":
    """同快照内序列号证据自相矛盾 → high；USB 桥正常形态 → None。"""
    disk = serials.get("disk_serial", "")
    phys = serials.get("physical_serial", "")
    raw = serials.get("smart_raw", "")
    source = (meta.get("serial_source") or "").strip()
    reasons: "list[str]" = []
    if phys and raw and phys != raw:
        reasons.append(f"physical_serial({phys}) 与 smart_raw({raw}) 不一致")
    if source == "smartctl" and disk and raw and disk != raw:
        reasons.append(f"serial_source=smartctl 但 disk_serial({disk}) "
                       f"与 smart_raw({raw}) 不一致")
    if (source == "smartctl" and disk and raw and disk != raw
            and not (phys and phys == raw)):
        reasons.append(f"serial_source=smartctl 但 disk_serial({disk}) "
                       f"与 smart_raw({raw}) 不一致")
    if disk and raw and disk != raw and not phys:
        # 无 physical_serial 佐证"桥上报值"解释 → disk_serial 与 smartctl
        # 读到的序列号矛盾无法排除串盘
        reasons.append(f"disk_serial({disk}) 与 smart_raw({raw}) 不一致，"
                       "且无 physical_serial 佐证桥上报值解释")
    if disk and phys and disk != phys and not (phys and raw and phys == raw):
        reasons.append(f"disk_serial({disk}) 与 physical_serial({phys}) 不一致"
                       "，且无法用 USB 桥形态解释")
    if not reasons:
        return None
    return {
        "type": "intra_snapshot_serial_drift",
        "severity": "high",
        "disk_id": disk_id,
        "snapshot_id": sid,
        "volume_id": vid,
        "serials": serials,
        "reasons": reasons,
        "suggestion": (
            f"快照 {sid} 内序列号证据自相矛盾（{'；'.join(reasons)}）；"
            "该盘可能是同型号双盘串盘，建议重新插拔并核对盘贴序列号后重采该快照"),
    }


def _audit_disk(disk_id: str,
                snaps: "list[tuple[str, str, dict | None]]",
                alerts: "list[dict]",
                notes: "list[dict]") -> None:
    # ① intra_snapshot_serial_drift：同快照内 smart 侧证据自相矛盾
    intra_sids: "set[str]" = set()
    for sid, vid, meta in snaps:
        if not meta:
            continue
        serials = _snapshot_serials(meta)
        # USB 桥正常形态：disk_serial 是桥上报值，physical_serial 与
        # smart_raw 一致（真盘序列号读取自洽）→ 信息性 notes，不告警
        if ("disk_serial" in serials and "physical_serial" in serials
                and serials["physical_serial"] == serials.get("smart_raw")
                and len(set(serials.values())) > 1):
            notes.append({
                "snapshot_id": sid, "volume_id": vid, "disk_id": disk_id,
                "issue": "usb_bridge_serial_form",
                "detail": (
                    f"disk_serial({serials['disk_serial']}) 为 USB 桥/外壳上报 ID，"
                    f"真盘序列号 {serials['physical_serial']} 读取自洽，属正常形态"),
            })
            continue
        alert = _intra_drift_alert(disk_id, sid, vid, meta, serials)
        if alert:
            alerts.append(alert)
            intra_sids.add(sid)

    # ② inter_snapshot_serial_volatility：同一卷的快照之间**同一字段**变化
    by_volume: "dict[str, list[tuple[str, dict]]]" = {}
    for sid, vid, meta in snaps:
        if not meta:
            continue
        serials = _snapshot_serials(meta)
        if serials:
            by_volume.setdefault(vid, []).append((sid, serials))
    for vid, items in sorted(by_volume.items()):
        # 逐字段比较：同一字段在不同快照间取值不同才告警（≥2 个可比较快照）
        for field in ("disk_serial", "physical_serial", "smart_raw"):
            values: "dict[str, list[str]]" = {}
            for sid, serials in items:
                v = serials.get(field)
                if v:
                    values.setdefault(v, []).append(sid)
            if len(values) < 2:
                continue
            involved = sorted({s for sids in values.values() for s in sids})
            # 去重：差异完全来自已有 intra 告警的快照 → 只报更具体的 intra
            if involved and all(s in intra_sids for s in involved):
                continue
            alerts.append({
                "type": "inter_snapshot_serial_volatility",
                "severity": "high",
                "disk_id": disk_id,
                "volume_id": vid,
                "field": field,
                "snapshot_ids": involved,
                "serials": {field: {v: sids for v, sids in values.items()}},
                "suggestion": (
                    f"同一卷 {vid} 的多次快照在 {field} 上出现了不同取值"
                    f"（{'/'.join(sorted(values))}）；"
                    "大概率是不同物理盘先后挂成了同一 volume_id，"
                    "建议按盘贴序列号拆分 volume_id 并重采"),
            })

    # ③ high_fallback_ratio / fallback_minority：走未验证回退的快照占比
    considered = [(sid, meta) for sid, _v, meta in snaps if meta]
    fallback = [(sid, meta) for sid, meta in considered
                if _snapshot_fallback_flags(meta)]
    if fallback:
        over_half = len(fallback) * 2 > len(considered)
        alerts.append({
            "type": "high_fallback_ratio" if over_half else "fallback_minority",
            "severity": "medium" if over_half else "low",
            "disk_id": disk_id,
            "snapshot_count": len(considered),
            "fallback_count": len(fallback),
            "snapshot_ids": [sid for sid, _ in fallback],
            "suggestion": (
                f"盘 {disk_id} 的 {len(fallback)}/{len(considered)} 个快照走了"
                "未验证回退（fallback-sd 候选 / 扫描映射未命中 / 卷序列号兜底）；"
                + ("设备定位可能错位，建议插单盘重采或开启身份校验后复核"
                   if over_half else
                   "占比未过半，属信息性提示：设备定位可能未经扫描映射核实，"
                   "建议下次采集开启身份校验")),
        })
