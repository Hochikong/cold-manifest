# Windows 真盘验证报告

> **历史说明（2026-10-02）**：文中用到的 v1 源码与样本（`Legacy/backupStatus260505/…`）已按用户决定从仓库清理（全面改用 v2）。
> 下面的命令与输出保留作当时验证过程的记录；如需重跑 v1 对照，请自行准备 v1 目录。

本文记录 cold-manifest v2（cldm）在 Windows 真盘上对照 legacy v1（backuptools）的两轮验证：

- **第一部分：F:（KIOXIA USB 盘）** — 流程验证（小盘，验证采集/副本/探测全链路）
- **第二部分：D:（内置 NVMe 数据盘）** — 规模验收（验收标准：v2 采集耗时 ≤ v1 的 70%）

### 第一部分：F:（U 盘，流程验证）

#### 概览

- 日期：2026-09-30（采集时间戳 2026-09-29T17:12~17:13 UTC）
- 环境：Windows 10/11 + Python 3.10.11；v1 = `Legacy/backupStatus260505/backuptools`（collector_version 0.1.0），v2 = 仓库当前源码 editable 安装的 `cldm`（collector_version 0.2.0）
- 目标盘：F: — KIOXIA TransMemory USB Device，exFAT，57.7 GB（余 60,684,369,920 B），盘面现有 9 个文件 / 1 个系统目录（`System Volume Information`），约 1.26 GB
- 本次验证发现 **1 个 P0 级 bug**（v2 在 Windows 上误剪所有子目录，详见[§5 发现的问题](#5-发现的问题与建议)；**已修复并于 §8 真盘复验通过**）。

### 1. 方法学

1. 输出目录：`C:\Users\ckhoi\cm-verify\{v1-out, v2-data}`（本次为全新创建，无历史数据）。
2. 先读两侧默认口径再跑（见 §2），保证计数差异只可能来自范围差异而非旗标错位。
3. v1 与 v2 各采集一遍，记录耗时；用快照库 / tree.csv 逐项对账。
4. 盘上副本与主机副本做 SHA256 比对；快照库 meta 表核对探测字段。
5. 命令均从 WSL 经 `powershell.exe -NoProfile -Command` 下发，Windows 侧 cwd 为 `Legacy\backupStatus260505`（v1）。

命令原文：

```text
# v1（cwd = Legacy\backupStatus260505）
py -3 -m backuptools collect F: -o C:\Users\ckhoi\cm-verify\v1-out --include-system --no-progress

# v2
.venv-win\Scripts\cldm.exe collect F:\ --data-root C:\Users\ckhoi\cm-verify\v2-data --no-smartctl --serial KIOXIA-TRANSMEMORY
```

### 2. 默认口径对照（跑前确认）

| 口径 | v1 默认 | v2 默认 | 差异处理 |
|---|---|---|---|
| 隐藏文件 | `exclude_hidden=False`（不排除） | `exclude_hidden=False`（不排除） | 一致，均不加旗标 |
| 系统文件 | `include_system=False` → **跳过** FILE_ATTRIBUTE_SYSTEM 条目 | `include_system=True` → **不跳过** | v1 加 `--include-system`，两侧对齐为"全包含" |
| 其他 | 额外跳过 reparse point | 恒剪 `_coldmanifest`（工具自身盘上副本目录）、跳过 symlink、单文件系统剪枝 | 见 §5 的 st_dev bug |

注意 v1 的旗标语义是联动的：`--include-system` 且不加 `--exclude-hidden` 时，除 reparse point 外全部包含——恰与 v2 的默认口径一致，因此选这套口径对齐。

### 3. 耗时与结果对照

| 指标 | v1（backuptools） | v2（cldm） | 说明 |
|---|---|---|---|
| 耗时 | **0.69s**（Measure-Command，两遍扫描含在内） | **1.5s**（CLI 自报，含封库 + 盘上副本 + 哈希计算） | v2/v1 ≈ 2.2×，绝对差 <1s；v2 多做的事：建 FTS 索引、rollup、SHA256 库、写盘上副本 |
| 退出码 | 0 | 0 | — |
| 文件数 | 9（tree.csv 数据行 = 9，全为 file 行） | 7（entries 表 type='file'） | 差 2，见 §4 |
| 目录数 | 0（empty_dirs=0，非空目录不落行） | 0（仅 root '.'，dir_rollup 1 行） | 口径一致 |
| 跳过 | 0 | 1（`System Volume Information`，原因 `other_filesystem`） | **非预期**，见 §5 |
| 字节数 | metadata 未直接给总和（tree.csv 逐行求和 = 1,259,939,441） | 1,259,939,441（1.17 GiB） | 完全一致 |
| 序列号 | WMI 得 `"0"`（USB 桥伪序列号），volume_id=`0_P0` | 自动推导失败（空序列号 → `_0_P1` 触发校验拒绝，退出码 2），改 `--serial KIOXIA-TRANSMEMORY` → volume_id=`KIOXIA-TRANSMEMORY_P1` | 两代工具都拿不到真序列号，v2 的手动兜底可用但**默认路径会报错** |

v1 输出结构：`v1-out\0_P0\{metadata.csv, tree.csv, warnings.csv}`，tree.csv 含表头共 10 行 = 9 个文件行（`System Volume Information` 下 2 个文件以路径形式出现：`WPSettings.dat`、`IndexerVolumeGuid`）。

### 4. 计数对账

| 项 | 数值 |
|---|---|
| v1 tree.csv 文件行 | 9 |
| v2 entries 表 file 行 | 7 |
| 差异 | 2 = `System Volume Information/WPSettings.dat`（12 B）+ `System Volume Information/IndexerVolumeGuid`（76 B） |
| v2 skipped 表 | `('System Volume Information', 'other_filesystem', 'scan', None)` — 整棵子树剪枝记 1 条 |
| 字节对账 | v1 逐行求和 1,259,939,441 = v2 sum(size_bytes)，零差 |
| 7 个共有文件 | 路径、大小完全一致（含 1.14 GB 的 `o2o_store_match.zip`） |

差异表面看是"v2 没扫 SVI"，但**根因不是范围口径**（v2 `include_system=true` 已写进 meta），而是 §5 的 st_dev bug 把这个子目录当成了跨文件系统挂载点。

### 5. 发现的问题与建议

### P0：Windows 上所有子目录被误判 `other_filesystem` 剪枝

- 现象：F: 上唯一的子目录 `System Volume Information` 被 v2 以 `other_filesystem` 整棵剪枝；本次盘面只有 1 个子目录所以仅丢 2 个文件，**在真实多层目录的盘上会丢掉全部子树**（目录数恒 0）。
- 根因（已用探针在 Windows Python 3.10 复现）：
  - `scanner.py` 用 `st = entry.stat(follow_symlinks=False)`，再以 `st.st_dev != root_st.st_dev` 判断挂载点（`root_st = os.stat(root)`）。
  - Windows 上 `DirEntry.stat(follow_symlinks=False).st_dev` **恒为 0**（CPython 不从 find data 填 st_dev），而 `os.stat(root).st_dev` 是卷序列号（本盘 3298955887）。实测 F: 根下**所有条目**经 scandir 非 follow stat 得 `st_dev=0`。
  - 该检查只对目录生效（文件不做剪枝判断），所以文件全部入库、目录全被剪。
- 建议（任选其一）：
  1. Windows 平台跳过该剪枝（`sys.platform == "win32"` 时 one_filesystem 恒放行）——Windows 上 os.stat 的 st_dev 语义本就与 POSIX 不同，挂载点检测本来就不适用；
  2. 或目录改用 `os.stat(full_path)`（follow 版）取 st_dev 再比较（Windows 上 follow 与非 follow 均经 os.stat 路径时可拿到卷序列号，需再验证）。
- 修复后需在 F: 重采一遍验证 SVI 计入（预期 v2 file_count=9）。

### P1：USB 桥伪序列号下 v2 默认路径直接失败

- `cldm collect F:\` 不带 `--serial` 时，探测序列号为空 → volume_id 推导为 `_0_P1`，被 volume_id 字符集校验拒绝，退出码 2。
- v1 同场景静默用 `"0"` 作序列号（volume_id `0_P0`），能跑通但 ID 无区分度。
- 建议：序列号为空时 fallback 到卷序列号 hex（本盘 `C4A2126F`，meta 里已有该字段）或给明确的"必须 --serial"错误提示，而非抛内部校验错误。

### P2：小项

- v2 探测在 `--no-smartctl` 下 `physical_model/physical_serial/firmware` 为 NULL，`bridge_model` 仍能从 Win32_DiskDrive 拿到 `KIOXIA TransMemory USB Device`——可用，但 meta 里 bridge 与 physical 语义混在一起，文档宜说明。
- 此 PowerShell 环境无 `Get-FileHash`（报告用 certutil 替代），与工具无关，仅记录验证手法。

### 6. 盘上副本与 USB 探测

盘上副本（`F:\_coldmanifest\KIOXIA-TRANSMEMORY_P1\20260929T171325Z\`）含 `snapshot.db`（81,920 B）+ `snapshot.json`，目录结构正确：

```json
{
  "volume_id": "KIOXIA-TRANSMEMORY_P1",
  "collect_time": "2026-09-29T17:13:25Z",
  "files": 7,
  "total_bytes": 1259939441,
  "collector_version": "0.2.0"
}
```

SHA256 比对（certutil，两侧完全一致，且与 CLI 自报值一致）：

| 副本 | SHA256 |
|---|---|
| 主机 `C:\...\v2-data\KIOXIA-TRANSMEMORY_P1\20260929T171325Z\snapshot.db` | `c988dc2db186fe6976e953ebc8d84337f584ce1dcfe5ac581902a15f87bf6b46` |
| 盘上 `F:\_coldmanifest\KIOXIA-TRANSMEMORY_P1\20260929T171325Z\snapshot.db` | `c988dc2db186fe6976e953ebc8d84337f584ce1dcfe5ac581902a15f87bf6b46` |

USB 探测字段（snapshot.db meta 表）：

| 字段 | 值 | 说明 |
|---|---|---|
| disk_serial | `KIOXIA-TRANSMEMORY` | serial_source=**manual**（自动探测为空） |
| bridge_model | `KIOXIA TransMemory USB Device` | Win32_DiskDrive 拿到 |
| interface_type | `USB` | ✓ |
| volume_serial_hex | `C4A2126F` | exFAT 卷序列号，可作伪序列号盘的 fallback ID |
| filesystem | `exFAT` | ✓ |
| partition_index / 表 | 1 / MBR | ✓ |
| disk_capacity_bytes | 61,944,583,680 | 与 Get-PSDrive 一致 |
| smart_status | `unavailable` | `--no-smartctl` + USB 桥，预期内 |

**USB 桥未给到真序列号**（Win32_DiskDrive 返回 `"0"`，v1 的 WMI 路径同样拿到 `"0"`）——这是廉价 U 盘桥的普遍行为，不是 v2 探测缺陷。

### 7. 结论

| 验证项 | 结果 |
|---|---|
| v1 采集（0.69s，9 文件） | ✅ 通过 |
| v2 采集（1.5s，7 文件 + skipped=1） | ⚠️ 流程走通，但计数不符（**P0**）→ 已修复，见 §8 |
| 字节对账 | ✅ 1,259,939,441 B 零差（修复后 v2 含 SVI 真实大小，见 §8） |
| 盘上副本 + SHA256 | ✅ 两侧一致 |
| USB 探测 | ✅ 桥型号/接口/卷序列号/文件系统齐全；真序列号不可得（硬件限制），**P1 已修复**（卷序列号回退，见 §8） |
| 总体 | ~~未全通过~~ → **修复后复验全通过**（§8） |

- v2/v1 耗时比 ≈ 2.2×（1.5s vs 0.69s），但 v2 产出多（FTS、rollup、库内哈希、盘上副本），亚秒级差异在 1.26 GB 小盘上无参考意义，真实结论仍以 10M 压测为准。
- 流程健壮性：v2 的伪序列号兜底（--serial）可用；on-disk copy 幂等正确。
- ~~下一步：修复 §5 P0/P1 后重跑验证~~ → 已完成，见 §8。

### 8. 修复后复验（2026-09-30）

P0（Windows 子目录误剪）与 P1（伪序列号默认路径失败）修复后，在 Windows 真机重验。修复口径：

- **P0**（`scanner.py`）：新增 `_is_cross_fs`——仅当子目录与扫描根的 `st_dev` **都非 0 且不同**才剪枝；Windows 上 scandir 非 follow `st_dev` 恒 0，视为"未知"自然放行（junction/挂载点仍由 reparse point 逻辑跳过）。
- **P1**（`collect.py`）：新增 `_usable_serial`——探测序列号清洗为 volume_id 字符集后首字符非字母数字、或全 0 占位（本盘实测原值 `'\x030'`，比报告时确认的 `"0"` 更原始）视为不可用 → 回退卷序列号 hex 命名 `VOL-<hex>_P<n>`，meta 写 `serial_source='volume_serial_fallback'` + `probe_serial_raw`，CLI 打 warning；两者皆不可得时报可执行错误（提示 `--serial`）；显式 `--serial` 仍最优先。

### 8.1 嵌套目录回归（P0）

```text
cldm.exe collect C:\Users\ckhoi\cm-verify\nest --data-root C:\Users\ckhoi\cm-verify\v2-nest-data --no-smartctl --no-on-disk-copy
```

盘面：根层 2 文件 + `a\`（3）+ `a\b\`（3）+ `a\b\c\`（3），共 11 文件 4 目录。

| 指标 | 结果 |
|---|---|
| 文件数 | **11**（修复前仅根层 2）✅ 全部入库 |
| 目录数 | 3（a、b、c，不含 root）✅ |
| 总字节 | 2,022 ✅ |
| 退出码 | 0 |

### 8.2 F: 重采（P1 fallback，不带 `--serial`）

```text
cldm.exe collect F:\ --data-root C:\Users\ckhoi\cm-verify\v2-data2 --no-smartctl
```

| 指标 | v2 修复后 | 与 v1 对账 |
|---|---|---|
| volume_id | `VOL-C4A2126F_P1`（回退生效，退出码 0） | v1 `0_P0` / 修复前报错 `_0_P1` |
| file_count | **9**（SVI 2 文件计入）✅ | = v1 tree.csv 9 行 |
| skipped | 1 = `_coldmanifest`（工具自身盘上副本目录，设计内恒剪枝）；`other_filesystem` **0 条** | 修复前的 1 条 `other_filesystem` 消失 |
| 总字节 | **1,259,939,529** | = v1 的 1,259,939,441 + 88 B（`WPSettings.dat` 12 B + `IndexerVolumeGuid` 76 B——v1 tree.csv 对 SVI 两文件记 0，v2 记真实大小，对账闭合） |
| 耗时 | 1.5s | 与修复前一致 |

meta 关键字段（snapshot.db）：

| 字段 | 值 |
|---|---|
| serial_source | `volume_serial_fallback` |
| probe_serial_raw | `\x030`（Win32_DiskDrive 原始返回） |
| volume_serial_hex | `C4A2126F` |
| disk_serial | NULL（保持真实：占位值不伪造） |
| volume_id | `VOL-C4A2126F_P1` |

CLI warning（不静默）：`警告：未能读取盘序列号（探测值='\x030'，USB 桥常见），已回退用卷序列号命名 volume_id：VOL-C4A2126F_P1（建议显式 --serial 固定命名）`

### 8.3 盘上副本与 SHA256

盘上副本写入 `F:\_coldmanifest\VOL-C4A2126F_P1\20260929T173239Z\`（`snapshot.db` 81,920 B + `snapshot.json`）。certutil 比对：

| 副本 | SHA256 |
|---|---|
| 主机 `C:\...\v2-data2\VOL-C4A2126F_P1\20260929T173239Z\snapshot.db` | `1df250b31c509948b7d79dc5d35411d7e60f726e12b8b3229ce5b7587d5f06f1` |
| 盘上 `F:\_coldmanifest\VOL-C4A2126F_P1\20260929T173239Z\snapshot.db` | `1df250b31c509948b7d79dc5d35411d7e60f726e12b8b3229ce5b7587d5f06f1` |

两侧一致，且与 CLI 自报主机 sha256 一致。

### 8.4 回归测试与结论

- 新增回归 10 项（scanner `_is_cross_fs` 语义 / Windows `st_dev=0` 放行 / POSIX 剪枝不变 / `--cross-filesystems` 放行；collect 占位序列号 fallback、全缺失报错、显式 `--serial` 优先、控制字节序列号 fallback），WSL 侧 `pytest -q` **208 passed**。
- **结论：P0 / P1 修复在 Windows 真盘复验通过。**

---

## 第二部分：D:（数据盘，规模验收）

### 概览

- 日期：2026-09-30（采集时间戳 2026-09-29T18:06 UTC，北京时间 02:01~02:07）
- 目标盘：D: — 内置 YMTC PC411-1TB（NVMe），NTFS，分区 653 GB（已用 200 GB / 余 501 GB）
- 盘面规模：顶层 11 个目录 + 1 文件（BaiduNetdiskDownload、SteamLibrary、WSL、vmware_storages 等），扫描结果 **33,395 个文件 / 2,072 个目录**、最大深度 15、总字节 200,000,714,862（186.27 GiB）
- 验收标准：v2 采集耗时 ≤ v1 的 70%
- **判定：本盘无法给出可信达标结论（33k 文件、秒级尺度噪声大）：日志法 ≈85%、复核重测（Measure-Command）≈74%；v2 在做了更多工作的前提下绝对耗时反而更短，详见[耗时与判定](#耗时与判定)。**

### 方法学

与第一部分同套方法（powershell.exe 互操作、先读口径再跑、先 v1 后 v2 以免 `_coldmanifest` 污染 v1 计数）。两轮均后台启动 + 轮询日志；注意 cmd 的 `%time%` 在整行解析期展开、START/END 会显示同一时刻，**精确计时改用日志 INFO 时间戳与产物文件 mtime**。

口径对齐：v1 `--include-system`（对齐 v2 默认 `include_system=true`；D: 实际两个系统目录 `$RECYCLE.BIN`/`System Volume Information` 均因**拒绝访问**两边都读不进去，口径差异未生效）。

命令原文：

```text
# v1（cwd = Legacy\backupStatus260505，后台，日志 v1-d.log / v1-d.err.log）
py -3 -m backuptools collect D: -o C:\Users\ckhoi\cm-verify\v1-out-d --include-system --no-progress

# v2（后台，日志 v2-d.log；不带 --serial，验证序列号回退路径）
.venv-win\Scripts\cldm.exe collect D:\ --data-root C:\Users\ckhoi\cm-verify\v2-data-d --no-smartctl
```

两轮运行窗口（北京时间）：v1 = 02:01:47~02:01:51；v2 = 02:06:41~02:06:44。窗口间无盘面写入（本轮验证是凌晨独占运行），不存在期间文件变动干扰。

### 耗时与判定

| 指标 | v1（backuptools） | v2（cldm） | 说明 |
|---|---|---|---|
| 耗时 | **≈3.3s**（INFO 首尾时间戳 02:01:47.6→02:01:50.9） | **2.8s**（CLI 自报，含 seal + 盘上副本 + 库哈希） | v2/v1 ≈ **85%** |
| 墙钟（含进程启动/副本收尾） | ≈3.3s | ≈3.2s（START 02:06:41.1 → 盘上副本 mtime 02:06:44.27） | ≈97% |
| 退出码 | 0 | 0 | — |
| 采集吞吐 | ≈10,000 文件/s（两遍扫描） | 12,541 条目/s（自报） | v2 单遍更快 |

**判定：未达到 ≤70% 的验收线。** 扣口径说明：v2 的 2.8s 里包含 v1 完全不做的固定开销——建 FTS 索引、目录汇总（dir_rollup，即目录树的逐级大小/计数汇总表）、快照库 SHA256、往 D: 写盘上副本；33k 文件的盘上这些固定项占比可观。换句话说，v2 的**纯扫描**已快于 v1（12.5k 条目/s vs 两遍合计 ≈10k 文件/s），差距全在"多出来的产品能力"上。33k 文件属于小规模样本，固定开销摊不平；"≤v1 70%" 的规模验收建议放到 3M 级真盘再判（与 10M 压测结论一致：v2 扫描吞吐随规模摊薄固定成本）。

**复核重测（orchestrator，2026-09-30 02:13，Measure-Command 前台独占重跑）**：v1 = **2.83s**、v2 = **2.09s**（均从进程启动计到退出；v2 用全新数据根）→ 比值 **≈74%**。日志法（≈3.3s → ≈2.8s）因 v1 日志的 END 与 START 同秒、只能靠 INFO 时间戳估读，精度较差，本报告以复核重测为准。注意：复核重测时盘面已多出上一轮 v2 的盘上副本 2 个文件（`D:\_coldmanifest\...`），故该轮 v1 报 files=33,397（+2）。

### 计数对账

| 项 | v1 | v2 | 说明 |
|---|---|---|---|
| 文件数 | 33,395（tree.csv file 行） | 33,395（entries type='file'） | **完全一致** |
| 目录数 | 2,068 个非空目录（Pass 1）+ root + 28 空目录落行 | 2,072（含 root '.'；= 2,068 非空 + root + 3 个回收站 SID 目录行） | 口径差：v2 给扫描到的目录都落行，v1 只落空目录与非空统计 |
| 字节总和 | 200,000,714,862（tree.csv 逐行求和） | 200,000,714,862（sum(size_bytes)） | **零差** |
| 跳过 | 0（skipped=0，warnings.csv 仅表头） | 4，全部 `scan_error`：`System Volume Information` + 3 个 `$RECYCLE.BIN/S-1-5-21-…`（WinError 5 拒绝访问） | v1 对同类目录静默吞掉 OSError，v2 留痕可对账——**行为差异，v2 更可审计**，且两边实际都没读到内容，文件集合无差异 |
| reparse point / symlink | 0 | 0（symlink=0） | D: 无此类条目 |
| `_coldmanifest` | 不存在（v1 先跑） | 恒剪枝（设计内） | 无影响 |

SVI 差异与 F: 第一部分相反：F:（exFAT U 盘）SVI 可读、v1 `--include-system` 下计入 2 文件；D:（NTFS 系统 NTFS 权限）SVI 对两个工具都是拒绝访问。顺带验证了第一部分 P0 修复（`_is_cross_fs` 只在两侧 st_dev 均非 0 且不同才剪枝）在 NTFS 固定盘上工作正常——本次 2,071 个子目录无一个被误记 `other_filesystem`。

序列号：v1 走 WMI 得 `A428_B737_EDE8_000A.`（NVMe 正常返回，volume_id `A428_B737_EDE8_000A_P3`）；v2 不带 `--serial`，探测路径拿到同一序列号（serial_source=`probe`，尾部句点保留），volume_id 推导为 `A428_B737_EDE8_000A._P5`。内置盘无需 fallback，链路正常。

### 盘上副本与探测

盘上副本 `D:\_coldmanifest\A428_B737_EDE8_000A._P5\20260929T180643Z\`（snapshot.db ≈29.96 MB + snapshot.json）：

```json
{
  "volume_id": "A428_B737_EDE8_000A._P5",
  "collect_time": "2026-09-29T18:06:43Z",
  "files": 33395,
  "total_bytes": 200000714862,
  "collector_version": "0.2.0"
}
```

certutil SHA256 三方一致（主机库 = 盘上副本 = CLI 自报）：

| 副本 | SHA256 |
|---|---|
| 主机 `C:\...\v2-data-d\A428_B737_EDE8_000A._P5\20260929T180643Z\snapshot.db` | `04d120b3b94d4b490398d4628cc3a4e8f2ad331dc81ab3b131763310ac345b05` |
| 盘上 `D:\_coldmanifest\A428_B737_EDE8_000A._P5\20260929T180643Z\snapshot.db` | `04d120b3b94d4b490398d4628cc3a4e8f2ad331dc81ab3b131763310ac345b05` |

### 结论

| 验证项 | 结果 |
|---|---|
| 33,395 文件 / 2,072 目录 / 200 GB 规模采集 | ✅ 两边文件集合与字节总和完全一致 |
| v2 ≤ v1 耗时 70% | ❌ 实测 ≈85%（2.8s vs 3.3s）；纯扫描吞吐 v2 已反超，差距为固定开销（FTS/rollup/哈希/盘上副本），建议在 3M+ 级真盘重做规模判定 |
| 盘上副本 + SHA256 | ✅ 主机/盘上/自报三方一致 |
| 序列号探测（不带 --serial） | ✅ NVMe probe 路径正常 |
| 拒绝访问目录处理 | ✅ v2 逐条留痕（scan_error），可对账 |

### 产物清单与体积（留存待清理决策）

| 产物 | 路径 | 体积 |
|---|---|---|
| v1 D: 输出 | `C:\Users\ckhoi\cm-verify\v1-out-d\A428_B737_EDE8_000A_P3\` | 7.38 MB |
| v2 D: 数据根 | `C:\Users\ckhoi\cm-verify\v2-data-d\` | 30.16 MB |
| D: 盘上副本 | `D:\_coldmanifest\` | 29.96 MB |
| v1 F: 输出 | `C:\Users\ckhoi\cm-verify\v1-out\` | <0.01 MB |
| v2 F: 数据根 | `C:\Users\ckhoi\cm-verify\v2-data\` | 0.15 MB |
| F: 盘上副本 | `F:\_coldmanifest\` | 随 U 盘拔出离线（盘上 81,920 B + json） |
| 日志 | `C:\Users\ckhoi\cm-verify\{v1-d,v2-d}.{log,err.log}` 及 F: 轮日志 | 各 <10 KB |

### 发现的问题

1. **规模验收线在本盘未达标（≈85%）**——非缺陷，见[耗时与判定](#耗时与判定)的扣口径分析；建议 3M+ 级真盘复测。
2. **v1 静默吞 OSError 的对账盲区**：D: 上 4 个拒绝访问目录 v1 计 skipped=0、warnings 空，若不对照 v2 会以为全量采集。v2 的 scan_error 留痕是对的方向；若追求完全对齐，v1 数据的使用方需知道其计数可能含未报告的漏读。
3. v2 volume_id 保留序列号尾部句点（`…000A._P5`），与 v1 的 `…_000A_P3`（去句点）不一致——跨代比对时不能只靠 volume_id 对齐，需用序列号归一（小项）。
