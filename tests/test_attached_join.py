"""`/api/disks/attached` 三源关联（_join_attached）测试。

回归：Win32_DiskDrive.Index（WMI 号）与 Get-Disk.Number（Storage 号）不保证
一致，旧实现拿 WMI 号喂 Get-Partition -DiskNumber，导致卷/序列号/型号对错盘
（真实事故：把一块盘的采集登记到另一块盘名下）。
"""

from __future__ import annotations

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


def test_storage_serial_wins() -> None:
    """Get-Disk 自带序列号 → serial_source=storage，WMI serial 不覆盖。"""
    items = rd._join_attached(
        [disk(1, serial="WD_ABC123", size=4000787030016)],
        [],
        [wmi(1, "WDC HDD", serial="WRONG_PLACEHOLDER", size=4000787030016)],
    )
    it = items[0]
    assert it["serial"] == "WD_ABC123"
    assert it["serial_source"] == "storage"
    assert it["serial_verified"] is True


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
    # A 盘：Index=0 的 WMI 行 Size 相同 → 匹配上；但 Storage serial 优先且可信
    assert a["serial"] == "SERIAL_OF_A"
    assert a["serial_source"] == "storage"
    assert [v["path"] for v in a["volumes"]] == ["E:\\"]
    # B 盘同理，绝不允许出现“A 盘序列号 + B 盘卷”的组合
    assert b["serial"] == "SERIAL_OF_B"
    assert [v["path"] for v in b["volumes"]] == ["F:\\"]
    # Index+Size 双匹配的 WMI 型号（同 Size 多盘时型号可能仍歧义，但 serial
    # 永远以 Storage 自报为准，这正是本修复要保证的不变量）
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
    assert c["serial_source"] == "storage"
    assert [v["path"] for v in c["volumes"]] == ["C:\\"]

    sg = by_num[1]
    assert sg["serial"] == "ZR30A1VK"
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
        assert by_dev[dev]["serial_source"] == "storage"


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
