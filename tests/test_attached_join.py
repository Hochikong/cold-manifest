"""`/api/disks/attached` 三源关联（_join_attached）测试。

回归：Win32_DiskDrive.Index（WMI 号）与 Get-Disk.Number（Storage 号）不保证
一致，旧实现拿 WMI 号喂 Get-Partition -DiskNumber，导致卷/序列号/型号对错盘
（真实事故：把一块盘的采集登记到另一块盘名下）。
"""

from __future__ import annotations

import json

from cold_manifest.api import routes_disks as rd


def disk(number, serial="", name="", size=0, bus="SATA", status="Online"):
    return {"Number": number, "SerialNumber": serial, "FriendlyName": name,
            "BusType": bus, "Size": size, "OperationalStatus": status}


def part(disk_number, letter="", pn=1, size=0):
    return {"DiskNumber": disk_number, "DriveLetter": letter,
            "PartitionNumber": pn, "Size": size}


def wmi(index, model="", serial="", size=0, iface="SATA"):
    return {"Index": index, "Model": model, "SerialNumber": serial,
            "Size": size, "InterfaceType": iface}


def logical(device_id="C:", fs="NTFS", label="System"):
    return {"DeviceID": device_id, "FileSystem": fs, "VolumeName": label}


def test_perfect_agreement_wmi_verified() -> None:
    """WMI Index == Get-Disk Number 且 Size 一致 → 三源合并一条，
    Storage 无序列号时 serial 取自 WMI（wmi_verified）。"""
    items = rd._join_attached(
        [disk(0, size=500107862016, name="Disk0")],
        [part(0, "C", 1, 500107859968)],
        [wmi(0, "Samsung SSD", serial="S3Z8NB0K123456", size=500107862016)],
        [logical("C:")],
    )
    assert len(items) == 1
    it = items[0]
    assert it["device"] == "\\\\.\\PhysicalDrive0"
    assert it["model"] == "Samsung SSD"
    assert it["serial"] == "S3Z8NB0K123456"
    assert it["serial_source"] == "wmi_verified"
    assert it["serial_verified"] is True
    assert it["wmi_matched"] is True
    assert [v["path"] for v in it["volumes"]] == ["C:\\"]
    assert it["volumes"][0]["filesystem"] == "NTFS"
    assert it["volumes"][0]["label"] == "System"


def test_storage_serial_fallback_when_wmi_unavailable() -> None:
    """WMI 不可得/未通过 Index+Size 双匹配 → 回退 Get-Disk 自报（storage）。

    Get-Disk 的序列号在部分 USB 桥上是盒子 ID 而非真盘序列号，所以它只是
    回退位：只有 WMI 双匹配失败时才采用。"""
    # WMI 缺失
    items = rd._join_attached(
        [disk(1, serial="WD_ABC123", size=4000787030016)], [], [])
    it = items[0]
    assert it["serial"] == "WD_ABC123"
    assert it["serial_source"] == "storage"
    assert it["serial_verified"] is True
    # WMI Index 命中但 Size 不符 → 双匹配失败，同样回退 storage
    items2 = rd._join_attached(
        [disk(1, serial="WD_ABC123", size=4000787030016)],
        [],
        [wmi(1, "WDC HDD", serial="WRONG_PLACEHOLDER", size=999)],
    )
    it2 = items2[0]
    assert it2["serial"] == "WD_ABC123"
    assert it2["serial_source"] == "storage"
    assert it2["serial_verified"] is True


def test_mismatched_index_size_no_cross_wiring() -> None:
    """错位：WMI Index≠Number 但 Size 相同 → 绝不张冠李戴。

    B 盘的 WMI 行（Index 恰好撞上 A 盘 Number）不能把它的 serial/model
    配到 A 盘的卷上；Index 命中但 Size 不符 → 只当诊断，不进 serial。
    """
    size_a = 500107862016
    items = rd._join_attached(
        [disk(0, serial="SERIAL_OF_A", size=size_a, name="Disk A"),
         disk(1, serial="SERIAL_OF_B", size=size_a, name="Disk B")],
        [part(0, "E", 1, size_a - 1024**2), part(1, "F", 1, size_a - 1024**2)],
        # WMI 号整体错位一行：Index=1 的行其实是 A 盘（Size 相同的经典歧义）
        [wmi(0, "Model B", serial="B_PLACE", size=size_a),
         wmi(1, "Model A", serial="A_REAL", size=size_a)],
        [logical("E:"), logical("F:")],
    )
    a = next(i for i in items if i["disk_number"] == 0)
    b = next(i for i in items if i["disk_number"] == 1)
    # 两块盘的 WMI 行 Index+Size 双匹配都通过（同 Size 多盘时这正是歧义所在），
    # 按新优先级 serial 取 WMI 值
    assert a["serial"] == "B_PLACE"
    assert a["serial_source"] == "wmi_verified"
    assert [v["path"] for v in a["volumes"]] == ["E:\\"]
    # 卷始终按 Get-Disk.Number 关联，绝不允许出现“A 盘序列号 + B 盘卷”
    assert b["serial"] == "A_REAL"
    assert b["serial_source"] == "wmi_verified"
    assert [v["path"] for v in b["volumes"]] == ["F:\\"]
    # 双匹配的 WMI 型号随行取用
    assert b["model"] == "Model A"
    # 反向情形：Storage 无 serial 且 Index 命中但 Size 不符 → serial 留空
    items2 = rd._join_attached(
        [disk(2, size=1000204886016)],
        [part(2, "G")],
        [wmi(2, "Some Disk", serial="MAYBE_OTHER_DISK", size=2000398934016)],
    )
    it = items2[0]
    assert it["serial"] == ""
    assert it["serial_verified"] is False
    assert it["serial_source"] == ""
    assert it["serial_unverified_raw"] == "MAYBE_OTHER_DISK"
    assert [v["path"] for v in it["volumes"]] == ["G:\\"]


def test_no_wmi_storage_only_degrade() -> None:
    """WMI 缺失/为空 → Storage-only：serial 可能为空，卷列表仍正确。"""
    for wmi_rows in ([], None):
        items = rd._join_attached(
            [disk(3, size=1000204886016, name="HDD")],
            [part(3, "D", 2, 999)], wmi_rows, [logical("D:", "exFAT", "Data")])
        assert len(items) == 1
        it = items[0]
        assert it["serial"] == ""
        assert it["serial_source"] == ""
        assert it["serial_verified"] is False
        assert it["model"] == "HDD"  # 退化用 FriendlyName
        assert [v["path"] for v in it["volumes"]] == ["D:\\"]
        assert it["volumes"][0]["filesystem"] == "exFAT"


def test_placeholder_serials_rejected() -> None:
    """占位序列号（0123456789ABCDEF / 全 F / 全 0）→ serial 留空 + 未验证。"""
    items = rd._join_attached(
        [disk(0, serial="0123456789ABCDEF", size=1),
         disk(1, serial="FFFFFFFF", size=2),
         disk(2, serial="0000000000", size=3)],
        [], [])
    by_num = {i["disk_number"]: i for i in items}
    for n in (0, 1, 2):
        assert by_num[n]["serial"] == ""
        assert by_num[n]["serial_verified"] is False
        assert by_num[n]["serial_source"] == ""
    # 正常序列号不受影响
    ok = rd._join_attached([disk(4, serial="S4E5NX0R98765", size=4)], [], [])[0]
    assert ok["serial"] == "S4E5NX0R98765"
    assert rd._is_placeholder_serial("") is False  # 空是缺失不是占位


def _realistic_user_fixture() -> "tuple[list, list, list, list]":
    """用户真实机器形态：4 块盘（NVMe C: / 希捷酷鹰 4T 数据盘 / USB 桥 8T /
    JMicron 桥空盘），WMI 号与 Storage 号在 USB 桥上错位一行，
    JMicron 桥报典型占位序列号 0123456789ABCDEF。"""
    nvme = 500107862016
    seagate = 4000787030016
    usb8t = 8001563222016
    jms = 4000787030016
    disks = [
        disk(0, serial="0025_3844_51B1_4EFD.", name="SAMSUNG MZVL81T0HELB-00BTW",
             size=nvme, bus="NVMe"),
        disk(1, serial="ZR30A1VK", name="Seagate Exos X16 4T", size=seagate),
        disk(2, serial="", name="USB SATA Bridge", size=usb8t, bus="USB"),
        disk(3, serial="0123456789ABCDEF", name="JMicron H/W RAID", size=jms,
             bus="USB"),
    ]
    parts = [
        part(0, "C", 1, nvme - 1024**3),
        part(1, "X", 1, seagate - 1024**3),
        part(2, "Y", 1, usb8t - 1024**3),
    ]
    wmi_rows = [
        wmi(0, "SAMSUNG MZVL81T0HELB-00BTW", serial="0025_3844_51B1_4EFD.",
            size=nvme),
        wmi(1, "Seagate Exos X16 4T", serial="ZR30A1VK", size=seagate),
        wmi(3, "USB SATA Bridge", serial="QRJ81K23", size=usb8t),  # WMI 号错位
        wmi(2, "JMicron Generic", serial="0123456789ABCDEF", size=jms),
    ]
    lds = [logical("C:"), logical("X:", "NTFS", "cold-4t"),
           logical("Y:", "NTFS", "cold-8t")]
    return disks, parts, wmi_rows, lds


def test_realistic_user_data_no_miswiring() -> None:
    """真实形态夹具：无错配——每条 serial/model 与自己的卷在同一块盘上，
    占位序列号不外泄，USB 桥 WMI 错位行仅作诊断。"""
    disks, parts, wmi_rows, lds = _realistic_user_fixture()
    items = rd._join_attached(disks, parts, wmi_rows, lds)
    assert len(items) == 4
    by_num = {i["disk_number"]: i for i in items}

    c = by_num[0]
    assert c["serial"] == "0025_3844_51B1_4EFD."
    assert c["serial_source"] == "wmi_verified"
    assert [v["path"] for v in c["volumes"]] == ["C:\\"]

    sg = by_num[1]
    assert sg["serial"] == "ZR30A1VK"
    assert sg["serial_source"] == "wmi_verified"
    assert [v["path"] for v in sg["volumes"]] == ["X:\\"]

    usb = by_num[2]
    # Storage 无 serial；WMI 里 Index=2 的是 JMicron 行（Size 不符）→ 不采用
    assert usb["serial"] == ""
    assert usb["serial_verified"] is False
    assert usb["serial_unverified_raw"] == "0123456789ABCDEF"
    assert [v["path"] for v in usb["volumes"]] == ["Y:\\"]

    jms = by_num[3]
    assert jms["serial"] == ""  # 占位序列号剔除
    assert jms["serial_verified"] is False
    assert jms["volumes"] == []

    # 任何条目都不得同时出现“他盘序列号 + 本盘卷”
    for it in items:
        assert it["serial"] != "QRJ81K23" or it["disk_number"] == 2


def test_user_machine_parity_index_number_agree() -> None:
    """用户真机夹具（实测：Get-Disk 与 Win32_DiskDrive 编号/序列号完全一致，
    0 Samsung NVMe / 1 WD Blue SN570 / 2 JMicron USB / 3 TOSHIBA USB）。

    加固不得引入偏差：这类"编号一致"的机器上，serial/model/卷 必须与旧
    （foreach Win32_DiskDrive）实现逐条相同。唯一有意差异：2 号 JMicron 的
    占位序列号 0123456789ABCDEF 判空（任务第 2 点要求），原始值进
    serial_unverified_raw。"""
    nvme, sn570, jms, tosh = (250059350016, 250059350016, 0, 1000204886016)
    disks = [
        disk(0, serial="0025_3844_51B1_4EFD.", name="Samsung SSD 970 EVO",
             size=nvme, bus="NVMe"),
        disk(1, serial="1C42AA1WSL1", name="WD Blue SN570", size=sn570),
        disk(2, serial="0123456789ABCDEF", name="JMicron Generic", size=jms,
             bus="USB"),
        disk(3, serial="Y6G90KFVS", name="TOSHIBA External USB", size=tosh,
             bus="USB"),
    ]
    parts = [part(0, "C", 1, nvme - 1024**3), part(1, "D", 1, sn570 - 1024**3),
             part(3, "E", 1, tosh - 1024**3)]
    wmi_rows = [
        wmi(0, "Samsung SSD 970 EVO 250GB", serial="0025_3844_51B1_4EFD.",
            size=nvme),
        wmi(1, "WD Blue SN570", serial="1C42AA1WSL1", size=sn570),
        wmi(2, "JMicron Generic", serial="0123456789ABCDEF", size=jms),
        wmi(3, "TOSHIBA External USB 3.0", serial="Y6G90KFVS", size=tosh),
    ]
    lds = [logical("C:", "NTFS", "System"), logical("D:", "NTFS", "Data"),
           logical("E:", "NTFS", "cold-toshiba")]
    items = rd._join_attached(disks, parts, wmi_rows, lds)
    assert len(items) == 4
    by_dev = {i["device"]: i for i in items}

    # 旧实现形态：device/model/serial/size_bytes/volumes 逐条等价
    old_shape = {
        "\\\\.\\PhysicalDrive0": {
            "model": "Samsung SSD 970 EVO 250GB",
            "serial": "0025_3844_51B1_4EFD.",
            "volumes": [("C:\\", "NTFS", "System")],
        },
        "\\\\.\\PhysicalDrive1": {
            "model": "WD Blue SN570", "serial": "1C42AA1WSL1",
            "volumes": [("D:\\", "NTFS", "Data")],
        },
        "\\\\.\\PhysicalDrive2": {
            "model": "JMicron Generic", "serial": "",  # 占位判空（有意差异）
            "volumes": [],
        },
        "\\\\.\\PhysicalDrive3": {
            "model": "TOSHIBA External USB 3.0", "serial": "Y6G90KFVS",
            "volumes": [("E:\\", "NTFS", "cold-toshiba")],
        },
    }
    for dev, exp in old_shape.items():
        it = by_dev[dev]
        assert it["model"] == exp["model"], dev
        assert it["serial"] == exp["serial"], dev
        assert [(v["path"], v["filesystem"], v["label"])
                for v in it["volumes"]] == exp["volumes"], dev
    # 占位那块：原始值保留诊断，正常三块可信
    j = by_dev["\\\\.\\PhysicalDrive2"]
    assert j["serial_unverified_raw"] == "0123456789ABCDEF"
    assert j["serial_verified"] is False
    for dev in ("\\\\.\\PhysicalDrive0", "\\\\.\\PhysicalDrive1",
                "\\\\.\\PhysicalDrive3"):
        assert by_dev[dev]["serial_verified"] is True
        assert by_dev[dev]["wmi_matched"] is True
        assert by_dev[dev]["serial_source"] == "wmi_verified"


def test_toshiba_box_id_vs_real_serial() -> None:
    """用户真实形态（4T 东芝移动盘）：Get-Disk.SerialNumber 报的是盒子 ID
    （20260123004775F），真盘序列号 16NDT0O1T 只有 ATA 直通/WMI 能读到。

    快选里必须优先展示真盘序列号：WMI Index+Size 双匹配通过 → 用 WMI 值。"""
    size = 4000787030016
    box_id, real = "20260123004775F", "16NDT0O1T"
    d = disk(4, serial=box_id, name="TOSHIBA External USB", size=size, bus="USB")

    # WMI 双匹配通过 → 真盘序列号胜出
    items = rd._join_attached(
        [d], [], [wmi(4, "TOSHIBA External USB 3.0", serial=real, size=size)])
    it = items[0]
    assert it["serial"] == real
    assert it["serial_source"] == "wmi_verified"
    assert it["serial_verified"] is True
    assert it["wmi_matched"] is True

    # WMI 缺失 → 回退存储层（此处只能是盒子 ID，但聊胜于无）
    it2 = rd._join_attached([d], [], [])[0]
    assert it2["serial"] == box_id
    assert it2["serial_source"] == "storage"
    assert it2["serial_verified"] is True

    # WMI Index 命中但 Size 不符 → 双匹配失败，同样回退 storage
    it3 = rd._join_attached(
        [d], [], [wmi(4, "TOSHIBA External USB 3.0", serial=real, size=999)])[0]
    assert it3["serial"] == box_id
    assert it3["serial_source"] == "storage"

    # 占位序列号（两侧都报 0123456789ABCDEF）→ serial 留空 + 原值进 raw
    it4 = rd._join_attached(
        [disk(5, serial="0123456789ABCDEF", size=1)],
        [],
        [wmi(5, "JMicron Generic", serial="0123456789ABCDEF", size=1)])[0]
    assert it4["serial"] == ""
    assert it4["serial_source"] == ""
    assert it4["serial_verified"] is False
    assert it4["serial_unverified_raw"] == "0123456789ABCDEF"


def test_real_size_tolerance_same_disk() -> None:
    """真机数值夹具：Get-Disk 与 Win32_DiskDrive 对同一块盘报的 Size 并不
    相等（4T 东芝差 ~2.6MB；JMicron 盒差 ~2.6MB），容差匹配须通过；
    且这两块 USB 盒在两个 API 里报的都是盒子 ID——序列号留系统枚举值时
    由前端标注"系统枚举 ID，可能是盒子 ID"，后端只保证不冒充真盘序列号。"""
    # 4T 东芝：Get-Disk 4000787027968 vs Win32 4000784417280
    gd_4t, wmi_4t = 4000787027968, 4000784417280
    assert rd._size_matches(gd_4t, wmi_4t)
    items = rd._join_attached(
        [disk(3, serial="20260123004775F", name="TOSHIBA External USB",
              size=gd_4t, bus="USB")],
        [],
        [wmi(3, "TOSHIBA External USB 3.0", serial="20260123004775F",
             size=wmi_4t)])
    it = items[0]
    # 两个 API 报的都是盒子 ID → WMI 双匹配通过但值相同，仍是盒子 ID；
    # wmi_matched=True（对号），可识别性交由前端标注
    assert it["serial"] == "20260123004775F"
    assert it["serial_source"] == "wmi_verified"
    assert it["wmi_matched"] is True
    assert it["size_bytes"] == gd_4t

    # JMicron 盒：2000398934016 vs 2000396321280，两侧都是占位号
    gd_jms, wmi_jms = 2000398934016, 2000396321280
    assert rd._size_matches(gd_jms, wmi_jms)
    it2 = rd._join_attached(
        [disk(2, serial="0123456789ABCDEF", name="JMicron Generic",
              size=gd_jms, bus="USB")],
        [],
        [wmi(2, "JMicron Generic", serial="0123456789ABCDEF", size=wmi_jms)])[0]
    assert it2["serial"] == ""
    assert it2["serial_verified"] is False
    assert it2["serial_unverified_raw"] == "0123456789ABCDEF"
    assert it2["wmi_matched"] is True

    # 容差之外仍拒绝：不同容量（>1% 且 >16MB）不得匹配
    assert not rd._size_matches(4000787027968, 500107862016)
    assert not rd._size_matches(8001563222016, 4000787027968)
    # 恰好超绝对上限（16MB+1）也不行
    assert not rd._size_matches(2000398934016, 2000398934016 + 16 * 1024 * 1024 + 1)


def test_smartctl_letter_first_real_fixtures(monkeypatch) -> None:
    """真机夹具：smartctl 直接吃盘符——`smartctl -i -j C:` 返回精确型号 +
    真序列号（与 /dev/sdX 一致）。快选取数盘符优先，scan-open 映射只是
    无盘符/打不开时的退路；容量只用于排除不符。"""
    reads: "list[tuple]" = []

    def fake_read(device, dtype, expect_size):
        reads.append((device, dtype, expect_size))
        if device == "C:" and dtype == "":
            return {"serial": "YMB51T0RA2534100PL", "model": "YMTC",
                    "device": "C:", "device_type": ""}
        if device == "E:" and dtype == "":
            return {"serial": "BTKA23811GZ2512A", "model": "INTEL SSD",
                    "device": "E:", "device_type": ""}
        return None  # -d sat 对盘符不适用等情形 → 链内下一类型/下一盘符

    monkeypatch.setattr(rd, "_smartctl_read_identity", fake_read)
    monkeypatch.setattr(rd, "_smartctl_scan_entries",
                        lambda: (_ for _ in ()).throw(
                            AssertionError("盘符可读就不该碰 scan-open")))
    items = rd._join_attached(
        [disk(0, serial="20260123004775F", name="Host SSD", size=250059350016),
         disk(4, serial="BOXID000000", name="INTEL USB", size=500107862016,
              bus="USB")],
        [part(0, "C", 1, 1), part(4, "E", 1, 1)],
        [])
    rd._smartctl_attach_serials(items, [])
    by_num = {i["disk_number"]: i for i in items}

    c = by_num[0]
    assert c["serial"] == "YMB51T0RA2534100PL"
    assert c["serial_source"] == "smartctl"
    assert c["serial_verified"] is True
    assert c["system_serial"] == "20260123004775F"

    e = by_num[4]
    assert e["serial"] == "BTKA23811GZ2512A"
    assert e["serial_source"] == "smartctl"
    assert e["serial_verified"] is True

    # 首选就是裸盘符 -i -j C:（类型兜底链在裸盘符失败后才启用）
    assert reads[0] == ("C:", "", 250059350016)
    assert any(r[0] == "E:" and r[1] == "" for r in reads)


def test_smartctl_letter_fallback_to_scan(monkeypatch) -> None:
    """盘符打不开 → 退回 scan-open 映射 + 容量复核；scan 也无果 → 回退
    系统值 + verified=False + 警告一次。"""
    scan_called: "list" = []

    def fake_read(device, dtype, expect_size):
        if device == "/dev/pd2":
            return {"serial": "X0DG6A2GS", "model": "JMicron",
                    "device": "/dev/pd2", "device_type": "usbjmicron"}
        return None  # 盘符 X: 等一律打不开

    monkeypatch.setattr(rd, "_smartctl_read_identity", fake_read)

    def fake_scan():
        scan_called.append(1)
        return [{"name": "/dev/pd2", "type": "usbjmicron"}]

    monkeypatch.setattr(rd, "_smartctl_scan_entries", fake_scan)
    items = rd._join_attached(
        [disk(2, serial="0123456789ABCDEF", name="JMicron Generic",
              size=2000398934016, bus="USB")],
        [part(2, "X", 1, 1)], [])
    warns: list = []
    rd._smartctl_attach_serials(items, warns)
    # 盘符 X: 打不开 → scan 映射读到 ATA 值
    assert scan_called
    assert items[0]["serial"] == "X0DG6A2GS"
    assert items[0]["serial_source"] == "smartctl"
    assert items[0]["serial_verified"] is True
    assert items[0]["system_serial"] == ""  # 系统侧是占位号，无系统值可留
    assert items[0]["serial_unverified_raw"] == ""  # 无 WMI 行，raw 本就为空
    assert warns == []

    # scan 也无果（映射不上）→ 回退系统值 + verified=False + 警告
    monkeypatch.setattr(rd, "_smartctl_scan_entries", lambda: [])
    items2 = rd._join_attached(
        [disk(2, serial="0123456789ABCDEF", name="JMicron Generic",
              size=2000398934016, bus="USB")],
        [part(2, "X", 1, 1)], [])
    warns2: list = []
    rd._smartctl_attach_serials(items2, warns2)
    # 占位号在 _join_attached 已判空：serial 留空 + verified=False + 警告
    assert items2[0]["serial"] == ""
    assert items2[0]["serial_verified"] is False
    assert warns2 and "smartctl 不可用" in warns2[0]


def test_smartctl_attach_real_fixtures(monkeypatch) -> None:
    """快选层 ATA 真序列号夹具（用户三块盘）：
    4T 东芝 → 16NDT0O1T；JMicron 盒 → X0DG6A2GS；WD → 23024Q800919。

    serial 优先 ATA 值（smartctl/verified=True），系统枚举值（盒子 ID）
    挪进 system_serial；_join_attached 的中间值经 _smartctl_attach_serials
    就地增强。"""
    scan = [{"name": "/dev/pd1", "type": "sat"},
            {"name": "/dev/pd2", "type": "usbjmicron"},
            {"name": "/dev/pd3", "type": "sat"}]
    monkeypatch.setattr(rd, "_smartctl_scan_entries", lambda: scan)
    idents = {
        1: {"serial": "23024Q800919", "model": "WD Blue SN570",
            "device": "/dev/pd1", "device_type": "sat"},
        2: {"serial": "X0DG6A2GS", "model": "JMicron Generic",
            "device": "/dev/pd2", "device_type": "usbjmicron"},
        3: {"serial": "16NDT0O1T", "model": "TOSHIBA External USB 3.0",
            "device": "/dev/pd3", "device_type": "sat"},
    }

    def fake_read(device, dtype, expect_size):
        for num, ident in idents.items():
            if device.endswith(f"pd{num}"):
                return ident
        return None

    monkeypatch.setattr(rd, "_smartctl_read_identity", fake_read)
    size = 4000787030016
    items = rd._join_attached(
        [disk(1, serial="1C42AA1WSL1", name="WD Blue SN570", size=250059350016),
         disk(2, serial="0123456789ABCDEF", name="JMicron Generic",
              size=2000398934016, bus="USB"),
         disk(3, serial="20260123004775F", name="TOSHIBA External USB",
              size=size, bus="USB")],
        [],
        [wmi(3, "TOSHIBA External USB 3.0", serial="20260123004775F",
             size=4000784417280)])
    rd._smartctl_attach_serials(items, [])
    by_num = {i["disk_number"]: i for i in items}

    wd = by_num[1]
    assert wd["serial"] == "23024Q800919"
    assert wd["serial_source"] == "smartctl"
    assert wd["serial_verified"] is True
    assert wd["system_serial"] == "1C42AA1WSL1"
    assert wd["model"] == "WD Blue SN570"

    jms = by_num[2]
    assert jms["serial"] == "X0DG6A2GS"
    assert jms["serial_source"] == "smartctl"
    assert jms["serial_verified"] is True
    # 占位号被 ATA 值顶掉，不再留档
    assert jms["serial_unverified_raw"] == ""

    tosh = by_num[3]
    assert tosh["serial"] == "16NDT0O1T"
    assert tosh["serial_source"] == "smartctl"
    assert tosh["serial_verified"] is True
    assert tosh["system_serial"] == "20260123004775F"  # 盒子 ID 留档给前端标注
    assert tosh["wmi_matched"] is True  # 容差复核仍成立


def test_smartctl_attach_fallbacks(monkeypatch) -> None:
    """无 smartctl / 非管理员 / 容量对不上 → 回退系统枚举值且 verified=False。"""
    size = 4000787030016
    base_items = lambda: rd._join_attached(  # noqa: E731
        [disk(3, serial="20260123004775F", name="TOSHIBA", size=size,
              bus="USB")],
        [],
        [wmi(3, "TOSHIBA External USB 3.0", serial="20260123004775F",
             size=size)])

    # ① smartctl 缺失/扫描失败 → 警告 + 全部 verified=False
    monkeypatch.setattr(rd, "_smartctl_scan_entries", lambda: [])
    items = base_items()
    warns: list = []
    rd._smartctl_attach_serials(items, warns)
    assert items[0]["serial"] == "20260123004775F"
    assert items[0]["serial_source"] == "wmi_verified"
    assert items[0]["serial_verified"] is False
    assert items[0]["system_serial"] == ""
    assert warns and "smartctl 不可用" in warns[0]

    # ② 读失败（非管理员/设备打不开，read 返回 None）→ 同样回退
    monkeypatch.setattr(rd, "_smartctl_scan_entries",
                        lambda: [{"name": "/dev/pd3", "type": "sat"}])
    monkeypatch.setattr(rd, "_smartctl_read_identity", lambda *a: None)
    items2 = base_items()
    rd._smartctl_attach_serials(items2, [])
    assert items2[0]["serial"] == "20260123004775F"
    assert items2[0]["serial_verified"] is False

    # ③ 容量复核不过（scan 下标撞上别的盘）→ read 内部已挡，这里验证映射兜底
    assert rd._smartctl_device_for({"disk_number": 1}, scan := [
        {"name": "/dev/sdz", "type": "sat"}]) is None  # 下标越界且无 pd 名


def test_smartctl_read_identity_unit(monkeypatch) -> None:
    """_smartctl_read_identity：-i -j 解析 / exit_status 位判 / 容量复核。"""
    import json as _json
    import subprocess

    class P:
        def __init__(self, payload, rc=0):
            self.stdout = (_json.dumps(payload).encode("utf-8")
                           if isinstance(payload, dict) else payload)
            self.stderr = b""
            self.returncode = rc

    def run_ok(cmd, **k):
        assert cmd[:1] == ["smartctl"] and cmd[-2:] == ["-i", "-j"]
        assert "-d" in cmd and "sat" in cmd
        return P({"serial_number": "16NDT0O1T",
                  "model_name": "TOSHIBA External USB 3.0",
                  "user_capacity": {"bytes": 4000784417280}})

    monkeypatch.setattr(subprocess, "run", run_ok)
    ident = rd._smartctl_read_identity("/dev/pd3", "sat", 4000787027968)
    assert ident is not None
    assert ident["serial"] == "16NDT0O1T"
    assert ident["model"] == "TOSHIBA External USB 3.0"
    # 容差复核：真机数值（差 ~2.6MB）通过，异盘容量被拒
    assert rd._smartctl_read_identity("/dev/pd3", "sat", 500107862016) is None

    # bit1 设备打不开（非管理员）→ None
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **k: P({}, rc=2))
    assert rd._smartctl_read_identity("/dev/pd3", "sat", 1) is None
    # bit4（健康告警位）不算致命
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: P({
        "serial_number": "Z9Y8X7W6", "user_capacity": {"bytes": 10}}, rc=8))
    assert rd._smartctl_read_identity("/dev/pd3", "sat", 10) is not None
    # ATA 序列号是占位号 → 视为没读到
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: P({
        "serial_number": "0123456789ABCDEF",
        "user_capacity": {"bytes": 10}}))
    assert rd._smartctl_read_identity("/dev/pd3", "sat", 10) is None


def test_smartctl_device_mapping() -> None:
    """scan-open 下标 + /dev/pdN 名字直配，每盘只出一个候选。"""
    entries = [{"name": "/dev/sda", "type": "sat"},
               {"name": "/dev/pd1", "type": "sat"},
               {"name": "/dev/sdc", "type": "usbjmicron"}]
    # pdN 名字直配优先
    assert rd._smartctl_device_for({"disk_number": 1}, entries) == entries[1]
    # 无 pd 名 → 下标兜底（盘号 0 → 第 0 条）
    assert rd._smartctl_device_for({"disk_number": 0}, entries) == entries[0]
    assert rd._smartctl_device_for({"disk_number": 2}, entries) == entries[2]
    # 越界且无 pdN → 无候选（回退系统值）
    assert rd._smartctl_device_for({"disk_number": 9}, entries) is None
    assert rd._smartctl_device_for({"disk_number": None}, entries) is None


def test_drive_letter_never_cached_or_persisted(monkeypatch) -> None:
    """盘符约束：①盘符只作当场读身份，同一盘符换盘后必须读到新盘的
    序列号（无跨请求"盘符→序列号"缓存）；②盘符绝不进任何身份字段
    （身份锚点只有序列号，响应项里不含盘符）。"""
    # 无任何模块级缓存结构（__cached__ 是 import 机制产物，忽略）
    assert not [n for n in vars(rd)
                if "cache" in n.lower() and n != "__cached__"]
    current = {"serial": "YMB51T0RA2534100PL"}  # C: 当场读到谁就是谁

    def fake_read(device, dtype, expect_size):
        if device == "C:":
            return {"serial": current["serial"], "model": "YMTC",
                    "device": device, "device_type": dtype}
        return None

    monkeypatch.setattr(rd, "_smartctl_read_identity", fake_read)
    monkeypatch.setattr(rd, "_smartctl_scan_entries", lambda: [])

    def one_request() -> dict:
        items = rd._join_attached(
            [disk(0, serial="SYS0", name="Host", size=1)],
            [part(0, "C", 1, 1)], [])
        warns: list = []
        rd._smartctl_attach_serials(items, warns)
        return items[0]

    first = one_request()
    assert first["serial"] == "YMB51T0RA2534100PL"

    # 同一盘符 C: 背后换了盘 → 下一次现场读必须拿到新序列号
    current["serial"] = "BTKA23811GZ2512A"
    second = one_request()
    assert second["serial"] == "BTKA23811GZ2512A"
    assert second["system_serial"] == "SYS0"

    # 盘符不得出现在任何身份字段（身份锚点只有序列号）
    for it in (first, second):
        blob = json.dumps({k: v for k, v in it.items() if k != "volumes"})
        assert "C:" not in blob
        for k in ("serial", "system_serial", "serial_unverified_raw"):
            assert not str(it[k]).endswith(":")
            assert it[k] != "C:"
        assert it["device"] == "\\\\.\\PhysicalDrive0"  # 设备串不含盘符


def test_attached_win_joins_and_degrades(monkeypatch) -> None:
    """_attached_win：正常 JSON 走 _join_attached；Get-Disk 空时 available:false。"""
    import json

    class P:
        returncode = 0

        def __init__(self, payload):
            self.stdout = json.dumps(payload).encode("utf-8")
            self.stderr = b""

    monkeypatch.setattr("subprocess.run", lambda *a, **k: P({
        "disks": [disk(0, serial="SN0", size=10, name="D0")],
        "partitions": [part(0, "C", 1, 9)],
        "logicalDisks": [logical("C:")],
        "wmi": [],
        "warnings": [],
    }))
    body = rd._attached_win()
    assert body["available"] is True
    assert body["items"][0]["device"] == "\\\\.\\PhysicalDrive0"
    assert body["items"][0]["serial"] == "SN0"
    assert body["items"][0]["volumes"][0]["path"] == "C:\\"

    monkeypatch.setattr("subprocess.run", lambda *a, **k: P({
        "disks": [], "partitions": [], "logicalDisks": [], "wmi": [],
        "warnings": ["Get-Disk: boom"],
    }))
    body = rd._attached_win()
    assert body["available"] is False
    assert "Get-Disk: boom" in body["reason"]
    assert body["items"] == []


def test_smartctl_calls_use_resolved_executable(monkeypatch) -> None:
    """回归：盘符直读与扫描都必须用 `smart.smartctl_exec()` 解析出的可执行文件。

    真机踩过：两处都曾写死 `["smartctl", ...]`——Windows 上 smartctl 通常不在
    PATH（用户按完整路径调用），于是 ATA 直读整条静默失败，快选退回系统枚举值
    （盒子 ID），"盘符直读真序列号"形同虚设。
    """
    import json as _json

    from cold_manifest import smart
    from cold_manifest.api import routes_disks as rd

    monkeypatch.setattr(smart, "smartctl_exec", lambda: "/opt/fake/smartctl")
    seen: "list[list[str]]" = []

    class _P:
        def __init__(self, payload):
            self.returncode = 0
            self.stdout = _json.dumps(payload).encode()
            self.stderr = b""

    def _run(cmd, **kw):
        seen.append(list(cmd))
        if "-i" in cmd:
            return _P({"serial_number": "X0DG6A2GS",
                       "model_name": "TOSHIBA HDWD120",
                       "user_capacity": {"bytes": 2000398934016}})
        return _P({"devices": [{"name": "/dev/sdc", "type": "sat"}]})

    monkeypatch.setattr("subprocess.run", _run)

    ident = rd._smartctl_read_identity("E:", "", 2000398934016)
    assert ident and ident["serial"] == "X0DG6A2GS"
    rd._smartctl_scan_entries()
    assert seen and all(c[0] == "/opt/fake/smartctl" for c in seen), seen
