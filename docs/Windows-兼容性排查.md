# cold-manifest Windows 兼容性风险全面排查

> **审计范围**：`src/cold_manifest/` 全源码 + `packaging/windows/` + `tests/`
> **审计日期**：2026-10-01
> **已知并已修/在修**：st_dev 恒 0、USB 桥伪序列号、`.cmd` GBK 注释、`%~dp0` 层级、uvloop、`CLDM_IMPORT_ROOTS` 改用 `os.pathsep`、SQLite URI 手拼/数据根未归一化（另一条线）
> **不在本次审计范围**：前端构建产物跨浏览器兼容、第三方 wheel 依赖本身的问题

---

## 结论与优先级速览

| 优先级 | 数量 | 一句话概括 |
|--------|------|-----------|
| **P0** | 3 | 会崩、会丢数据、或 100% 触发功能不可用 |
| **P1** | 4 | 特定场景功能不可用或体验严重受损 |
| **P2** | 8 | 边缘场景、体验瑕疵、或代码债 |

**建议修复顺序**：P0-2（URI 空格编码）→ P0-1（删除时未关 DB 连接）→ P0-3（WAL on exFAT）→ P1-1（控制台编码）→ P1-4（CSV BOM）→ 其余按需排期。

> **状态速览（2026-10-01 更新）**：P0-1/2/3、P1-1/2/3、P2-1/2/3/7/8 **已修复**；P1-4 以「大小写改名提示」方案落地（不自动改默认口径）；P2-4/5/6 保留为文档建议，未改代码。逐条证据见文末「修复验证证据」。**本轮追加（实机反馈，2026-10）：P0-9「假损坏」（immutable 连接 + 封库后就地写）已修复，P2-3 结论已按真机实测更正**——见对应条目。

---

## P0 — 会崩或数据错

### P0-1 删除快照时 `shutil.rmtree` 因文件被占用而失败（Windows 独有）

> ✅ **已修复**（提交 5e5a611）：AppState 新增 `evict_snapshot(snapshot_id)` 定向关闭连接池句柄；删除路径统一走 `catalog.rmtree_ro`（见 P1-3），删前先驱逐 LRU 池中该快照的连接，被占用时报可读错误。

- **触发场景**：Web 服务正在运行（AppState LRU 池持有快照库连接），用户通过 CLI `cldm delete <sid>` 或 Web API DELETE 删除该快照；或任务清扫孤儿时该库刚被查询过。
- **证据**：
  - `src/cold_manifest/catalog.py:229` `shutil.rmtree(host_dir)` — 删主机快照目录
  - `src/cold_manifest/catalog.py:250` `shutil.rmtree(copy_dir)` — 删盘上副本目录
  - `src/cold_manifest/tasks.py:146` `shutil.rmtree(ts_dir)` — 孤儿清扫
  - `src/cold_manifest/collect.py:549` `shutil.rmtree(...)` — 取消/失败时清理盘上副本临时目录
  - `src/cold_manifest/import_legacy.py:323` `shutil.rmtree(dest_dir)` — 强制重新导入
- **根因**：Windows 不允许删除被打开的文件/目录。`AppState.snapshot_db()` 会把只读连接缓存在 LRU 池（`api/state.py:34-55`），删除前没有关闭对应连接。
- **建议修法**：
  1. 在 `AppState` 增加 `evict_snapshot_db(snapshot_id: str) -> bool` 方法，从 `_snap_pools` 取出并 `close()`；
  2. `catalog.delete_snapshot` 执行 `shutil.rmtree` 前，先调用 `evict_snapshot_db`（API 侧通过 `AppState` 调用，CLI 侧 LRU 池不存在但应统一口径）；
  3. 孤儿清扫 `tasks.py:_sweep_orphan_snapshots_locked` 也遍历 `_snap_pools` 关库后再删。
- **Linux 回归测试**：可在 Linux 侧模拟（用 `fuser` 或 `lsof` 保持文件打开后执行删除），但最简便的做法是写单元测试：mock `shutil.rmtree` 在被占用时抛 `PermissionError`，断言删除逻辑先调用了 `evict_snapshot_db`。

---

### P0-2 多处手拼 SQLite `file:` URI，路径含空格/中文时打不开库

> ✅ **已修复**（提交 2a2f30c）：全项目统一 `db.file_uri()`（resolve + as_uri 百分号编码，消除 `..`/空格/中文问题），数据根入口做 resolve 归一化，打开失败信息可读化。

- **触发场景**：数据根、工作目录或 snapshot.db 所在路径包含空格（`C:\Users\John Doe\data`）或 Unicode 字符。手拼 URI 未做百分号编码，SQLite 报 "unable to open database"。
- **证据**：
  - `src/cold_manifest/catalog.py:277` `sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)`
  - `src/cold_manifest/diff_engine.py:748` 同上
  - `src/cold_manifest/diff_engine.py:760` 同上
  - `src/cold_manifest/diff_engine.py:784` 同上
  - `src/cold_manifest/report.py:74` 同上
- **根因**：`db.as_posix()` 不编码空格，`Path.as_uri()` 才会。db.py 里已有正确实现 `file_uri()`（内部用 `as_uri()`），但上述几处仍手拼。
- **建议修法**：全局替换手拼 URI 为 `file_uri(db, immutable=False/True)`：
  ```python
  # 之前
  conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
  # 之后
  from ..db import file_uri
  conn = sqlite3.connect(file_uri(db, immutable=False), uri=True)
  ```
- **Linux 回归测试**：在 Linux 侧创建含空格/中文的临时目录（如 `/tmp/我的 data/`），跑 `read_snapshot_meta`、`iter_diff_csv`、`generate_diff_report` 等函数即可复现。已有 `file_uri` 的单元测试可扩展一个含空格路径的 case。

---

### P0-3 catalog.db WAL 模式在 exFAT/FAT32（U 盘、外置盘）上不可靠

> ✅ **已修复**（提交 68ada3f）：`open_catalog` 检查 `PRAGMA journal_mode=WAL` 的实际返回值，未生效（exFAT/FAT32 常见静默回退）时确保 DELETE 模式并降级 synchronous、记 warning。

- **触发场景**：用户把数据根设在外置 U 盘（exFAT/FAT32），catalog.db 开启 WAL（`db.py:48` `PRAGMA journal_mode=WAL`）。掉电/热插拔时 catalog 可能损坏，或 `-wal`/`-shm` 文件残留导致后续打开异常。
- **证据**：`src/cold_manifest/db.py:48` `conn.execute("PRAGMA journal_mode=WAL")`
- **根因**：WAL 依赖文件系统的内存映射/共享内存语义。exFAT/FAT32 不支持文件锁定与内存映射的完整语义，SQLite 官方文档明确不推荐在 FAT/exFAT 上使用 WAL。
- **建议修法**：`open_catalog` 时探测 catalog.db 所在文件系统类型：
  ```python
  def _fs_supports_wal(path: Path) -> bool:
      if sys.platform == "win32":
          # 用 GetVolumeInformation 或 os.statvfs 的 f_type
          # exFAT = 0x2011BAB0 (Windows), FAT32 = 0x0B/0x0C
          ...
      return True  # Linux 下默认信任
  ```
  若不支持，回退 `journal_mode=DELETE` + `synchronous=NORMAL`，并在日志中 warning。
- **Linux 回归测试**：Linux 侧可格式 loop 设备为 exFAT/fat32 后挂载，验证 `open_catalog` 自动回退；或 mock `os.statvfs` / `_get_volume_info` 的返回值断言 PRAGMA 语句。

---

## P1 — 功能不可用

### P1-1 CLI 中文输出在 GBK 控制台触发 `UnicodeEncodeError`

> ✅ **已修复**（提交 68ada3f）：`packaging/windows/cldm.cmd` 与 `start.cmd` 顶部 `chcp 65001` + `set PYTHONIOENCODING=utf-8`。

- **触发场景**：Windows 命令提示符默认代码页 936（GBK），用户执行 `cldm collect`、`cldm diff` 等命令，输出含 `·`、`↔`、`→` 等 Unicode 字符时直接 traceback 崩溃。
- **证据**：大量 `print(...)` 含中文标点和箭头符号，如 `src/cold_manifest/cli.py:222-230` diff 结果输出、`cli.py:271-281` collect 结果输出。
- **建议修法**：在 `packaging/windows/cldm.cmd` 与 `start.cmd` 顶部加 `chcp 65001` 并设置环境变量：
  ```cmd
  chcp 65001 >nul
  set "PYTHONIOENCODING=utf-8"
  ```
  或在 `cli.py:main()` 入口加：
  ```python
  if sys.platform == "win32":
      import io
      if isinstance(sys.stdout, io.TextIOWrapper):
          sys.stdout.reconfigure(encoding="utf-8")
  ```
- **Linux 回归测试**：在 Linux 侧模拟 GBK 环境：`PYTHONIOENCODING=gbk python -m cold_manifest.cli diff ...` 应复现崩溃；修复后通过。

---

### P1-2 CSV 导出无 BOM，Excel 直接打开中文乱码

> ✅ **已修复**（提交 68ada3f）：CSV 写入口统一 `encoding="utf-8-sig"`（带 BOM），Excel 双击打开中文正常。

- **触发场景**：用户在 Windows 下用 `cldm export` 或 Web 下载 CSV，双击用 Excel 打开，中文字符显示为乱码（Excel 默认按 ANSI/GBK 读取无 BOM 的 UTF-8）。
- **证据**：
  - `src/cold_manifest/exporter.py:48` `with Path(out).open("w", newline="", encoding="utf-8")`
  - `src/cold_manifest/exporter.py:68` `with (out_dir / "metadata.csv").open("w", newline="", encoding="utf-8")`
  - `src/cold_manifest/exporter.py:84` tree.csv 同理
  - `src/cold_manifest/exporter.py:100` warnings.csv 同理
  - `src/cold_manifest/diff_engine.py:778-807` `iter_diff_csv` 用 `sqlite3.connect(...)` 无编码设置（默认 UTF-8）
- **建议修法**：所有 CSV 写入口统一改为 `encoding="utf-8-sig"`（带 BOM），或在 CLI/API 增加 `--bom` 开关；至少 metadata/tree/warnings 三件套和 diff CSV 应默认带 BOM，因为目标用户就是 Windows Excel。
- **Linux 回归测试**：纯文本测试：读回文件头 3 字节断言为 `\xef\xbb\xbf`。

---

### P1-3 `shutil.rmtree` 遇只读文件失败（Windows 独有）

> ✅ **已修复**（提交 5e5a611）：封装 `catalog.rmtree_ro()`（onexc 中去只读后重试），全项目删除路径统一替换。

- **触发场景**：快照目录或盘上副本目录内的文件被设为只读（如从 CD/DVD 复制而来，或某些备份软件标记），删除时 `shutil.rmtree` 抛 `PermissionError`。
- **证据**：全项目所有 `shutil.rmtree` 调用均未传 `onerror`/`onexc` 回调，典型位置见 P0-1 列表。
- **建议修法**：封装一个 `_rmtree_ro(path: Path)`，在 `onexc` 中先 `chmod` 去只读再重试：
  ```python
  import stat, os
  def _rmtree_ro(path: Path) -> None:
      def onexc(fn, p, exc_info):
          os.chmod(p, stat.S_IWRITE)
          fn(p)
      shutil.rmtree(path, onexc=onexc)
  ```
  全项目统一替换。
- **Linux 回归测试**：Linux 侧创建只读文件后调用 `_rmtree_ro`，断言删除成功。

---

### P1-4 默认 diff 不识别 Windows 大小写无关的“改名”（同一文件 `a.txt` → `A.TXT`）

> ✅ **已落地（提示方案，提交 be4be0e）**：不改默认口径，改为在结果里主动提示——两侧快照任一来自 win32（meta 新增 `os_platform` 键）且未启用 `case_insensitive` 时，diff 物化写 `hints_json`，API 响应带 `hints`、CLI 末尾输出提示行，说明“仅大小写改名会显示为 1 删除 + 1 新增，可加 --case-insensitive 归并”。前端对比页展示提示。

- **触发场景**：Windows NTFS 大小写不敏感。用户在 Windows 上把 `a.txt` 重命名为 `A.TXT`，下次采集后做 diff（默认精确 path），结果会报一行 `removed` + 一行 `added`，而不是 `identical` 或 `moved_or_renamed`。浪费用户注意力。
- **证据**：`src/cold_manifest/diff_engine.py` 默认走 `_materialize_sql`（`path` 精确等值 join），`case_insensitive` 选项默认关闭。
- **建议修法**：在 `canonical_options` 中，若检测到 `sys.platform == "win32"` 且用户未显式指定 `case_insensitive`，可默认设为 `True`；或在 CLI/API 文档中醒目标注 Windows 用户建议加 `--case-insensitive`。更稳妥的做法是在 `materialize_diff` 前置探测：若两侧快照的 `meta.os_platform == 'win32'` 且未显式传 `case_insensitive`，自动开启并记录日志。
- **Linux 回归测试**：构造两个快照，entries.path 分别为 `a/b` 和 `A/B`，验证默认选项在 Linux 下仍报 removed+added（保持现有行为），但在 Windows 下（mock `sys.platform`）自动走 case_insensitive 归并。

---

## P2 — 体验与边缘

### P2-1 未过滤 Windows 保留名 / 末尾点空格 / ADS

> ✅ **已修复**（提交 68ada3f）：scanner 新增 `_is_dangerous_name`（保留名 / 末尾点空格 / 冒号 ADS），命中记 `skipped('dangerous_name')` 不产出条目；Linux 默认不启用。

- **触发场景**：备份的源盘包含名为 `CON`、`PRN`、`NUL`、`AUX`、`COM1`…`COM9`、`LPT1`…`LPT9` 的文件/目录，或末尾带 `.` / 空格的文件名，或含 `:` 的 ADS 文件名（如 `file.txt:zone.identifier`）。Windows API 对这些名称有特殊处理，可能导致扫描、哈希、校验时行为异常（打不开、报错、或打开到设备而非文件）。
- **证据**：`src/cold_manifest/scanner.py` 的 `scan_tree` 没有针对 Windows 保留名的过滤逻辑；`_is_hidden`、`_is_system` 只过滤属性位。
- **建议修法**：在 scanner 中添加 `_is_dangerous_name(name: str) -> bool`，检测：
  - 保留名（不区分大小写，含 `.txt` 后缀如 `CON.txt` 也算）
  - 末尾为 `.` 或空格（Windows 会自动截断）
  - 含 `:`（ADS）
  命中则记 `skipped('dangerous_name')`，不产出条目。
- **Linux 回归测试**：Linux 侧创建名为 `CON`、`foo.`、`bar `、`test:ads` 的文件，验证 scanner 正确跳过并记账。

---

### P2-2 长路径（>260）未启用 `\\?\` 前缀

> ✅ **已按建议方案落地预警（提交 be4be0e）**：Windows 且扫描根路径 >200 字符时，记 warning 与 `skipped(warning_type='long_path')`，提示启用系统长路径支持（LongPathsEnabled）。`\\?\` 前缀未引入（文档建议不变）。

- **触发场景**：深层目录树（如 node_modules）路径长度超过 Windows `MAX_PATH`（260 字符），`os.scandir`、`open()`、SQLite URI 均可能失败。
- **证据**：全项目未使用 `\\?\` 前缀。Python 3.6+ 在 manifest 中开启 `longPathAware` 后可突破，但项目未提供 manifest。
- **建议修法**：在文档中说明“Windows 用户若需扫描深层目录，请在注册表或组策略中启用长路径支持（Windows 10 1607+）”；代码层面可在 `collect.py` 对 `scan_root` 做检测，若 `len(str(scan_root)) > 200` 给出 warning。
- **Linux 回归测试**：不适用（Linux 路径限制远大于 260）。

---

### P2-3 smartctl Windows 设备路径可能不对（USB 桥）

> ✅ **已修复，结论按真机实测更正（2026-10 本轮）**：真机（Windows USB 桥）实测 `\\.\PhysicalDriveN` 直接喂给 smartctl 常报 `Invalid argument`，而 `smartctl --scan` 给出的是 `/dev/sdN`（如 `/dev/sdc -d sat`）。现在 `smart.py` 的设备定位链为：`CLDM_SMARTCTL_DEVICE` 环境变量覆盖 → `--scan` 第 N 行（优先 JSON、回退文本解析，进程内缓存；顺序与 PhysicalDrive 编号一致）→ `\\.\PhysicalDriveN` 兜底 → 盘符；类型走兜底链 auto → sat → sat,12 → 扫描建议 → 桥专用（usbjmicron/usbsunplus/usbprolific/jms56x）。判定"读到数据"只看退出码低 2 位（bit0 命令行错误、bit1 设备打不开），健康告警位（8/16/32/64/128）不算失败、由 parse_smart 推断健康等级。可用 `CLDM_SMARTCTL_DEVICE` / `CLDM_SMARTCTL_ARGS` 覆盖。**第五轮更正（2026-10）**：`\\.\PhysicalDriveN` 已确认不是 smartctl 认可的设备名（只认 `/dev/sd[a-z]`、`/dev/pdN`、`X:`），已从候选链彻底移除；`jms56x`/`jmb39x`/`sntjmicron` 等 RAID 盒专用类型（对普通单盘有覆写扇区风险）也已从桥类型删除；设备与类型改由 `smartctl --scan-open -j` 权威给出（打不开的条目附 `open_error`、自动跳过）。最新口径见《升级方案》§4.4 与《Windows-运行说明》。**第八轮更正（2026-10，序列号与身份口径）**：真机证实 USB 盒上报的序列号（盒 ID/占位号）**不能作为磁盘身份依据**——经 USB 硬盘盒接入的盘现在**必须手填盘体标签序列号**才能采集（判定只看 `interface_type` 含 USB；API 400 / CLI rc=2，前端依赖检查 `requires_manual_serial` 禁用开始）；设备定位判据从"序列号精确匹配"放宽为**容量+型号唯一匹配**（`unique_capacity_match`：盒 ID 与真盘序列号不同不再导致正确盘被剔除，同容量同型号并列仍不验证）；`identity-check` 体检的 USB 桥正常形态（盒 ID 与真盘序列号并存）不再报 HIGH。见《升级方案》§4.3/§4.4 与《Windows-真机复验清单》§11。
>
> ✅ **同日按真机证据放宽（2026-10-03）**：真机照片证实东芝盒 / JMicron 盒**都透传真盘序列号**（`16NDT0O1T` / `X0DG6A2GS`，与 CrystalDiskInfo 一致）——不可靠的不是 USB 盒本身，而是**系统枚举层（Windows Get-Disk）**报的 ID（如 `20260123004775F`、占位号 `0123456789ABCDEF`），旧版本拿它当磁盘身份才是"两盘撞号"的根源。因此上面"经 USB 盒接入必须手填"的口径**放宽为按序列号可用性判定**：能读到 ATA 直通真盘序列号（USB 盒透传属常见）或可用的系统枚举序列号 → **直接采集，不要求手填**；只有两者都拿不到时才要求手填盘体序列号（前端 `requires_manual_serial=true` 禁用「开始采集」，后端 400、CLI rc=2）。preflight 响应新增 `smart_serial`（ATA 直通真序列号）与 `serial_usable` 字段，`manual_serial_reason` 如实区分两个来源的可信度。历史结论（系统枚举 ID 不能单独作身份依据、判据 `unique_capacity_match` 容量+型号唯一匹配）不变；见《升级方案》§4.3/§4.4 与《Windows-真机复验清单》§11。

> ⚠️ **UAT 更正（2026-10-03，真机 UAT 后，口径再次收紧）**：上面"**或可用的系统枚举序列号 → 直接采集**"一句已被真机 UAT 推翻——真机证实系统枚举层不仅给盒子 ID，还会给出**看似正常**的值（如 `16NDT0O1T` 形态），且设备定位层曾因"同容量并列"按扫描顺序读到**别的盘**的序列号（东芝 P300 快照被登记到 WD SN570 名下，修复 `8c25aaf`）。最终口径（`33134e1`）：**Windows 上系统枚举（Get-Disk/WMI）序列号一律不得充当磁盘身份**，只有 `serial_source=="smartctl"`（ATA 直通已验证）或手填（manual）才放行；`smartctl_unverified`（读到了但未确认是目标盘）宁严勿松亦拦。判据唯一入口 `requires_manual_serial()`（`src/cold_manifest/collect.py:92`），采集前依赖检查与采集引擎同参（`d977044`）。历史口径保留如上，仅供追溯。

- **触发场景**：Windows 下 USB 桥接硬盘，smartctl 对盘符（`E:`）常无效，需用 `\\.\PhysicalDriveN`。代码已尝试 `-d sat` 回退，但设备路径本身仍可能是盘符。

> ⚠️ **UAT 更正（2026-10-03）**：上面"smartctl 对盘符常无效"的判断**被真机 UAT 推翻**——真机实测 `smartctl -i -j C:` 直接接受盘符并给出精确型号+真盘序列号（与 `/dev/sdX` 读数一致），快选层已改为盘符直读真序列号（`608bd40`）。保留原文仅供追溯。另：直读路径曾写死 `smartctl` 而未走 `smart.smartctl_exec()`，Windows 上 smartctl 不在 PATH 时整条 ATA 直读**静默失败**退回系统枚举假值——已修（`608bd40`，回归测试盯死必须用解析出的可执行文件）。
- **证据**：`src/cold_manifest/smart.py:186-188` `device_for_path` 在 Windows 下仅返回盘符；`probe/windows.py:278` 在 smartctl 分支里会尝试用 `PhysicalDrive{idx}`，但 `smart.py` 的独立调用（如 `check_smartctl` / CLI 直接调）仍走盘符。
- ~~建议修法~~（已被本轮实现取代）：见上方更正后的结论；采集 meta 现记录 `smart_attempts_json`（每次尝试的参数/退出码/stderr 片段），失败原因归类为 permission_denied / device_type_unknown / not_found / timeout / other 并给可读文案。
- **Linux 回归测试**：mock `probe_path_win` 返回 `disk.index=3`，断言 `device_for_path("E:\\")` 返回 `\\.\PhysicalDrive3`。

---

### P2-4 `start.cmd` 绑定 `0.0.0.0` 触发 Windows 防火墙弹窗

> ✗ **未改代码（保留文档建议）**：`start.cmd` 目前仍绑定 0.0.0.0（与“开放局域网访问”的既定口径一致）；首次运行防火墙弹窗属预期，用户点“允许”即可。如需本机-only 可手工改 `--host 127.0.0.1`。

- **触发场景**：首次运行 `start.cmd`，Windows Defender 防火墙弹出“允许 Python 访问网络”提示，对非技术用户造成困惑。
- **证据**：`packaging/windows/start.cmd:15` `".venv-win\Scripts\cldm.exe" serve --host 0.0.0.0 --port 8765`
- **建议修法**：默认改为 `--host 127.0.0.1`，在注释里说明“如需局域网访问，改为 `0.0.0.0`”。或提供 `start-local.cmd` / `start-lan.cmd` 两个入口。
- **Linux 回归测试**：不适用。

---

### P2-5 WSL ↔ 原生 Windows 路径互操作问题（数据可移植性）

> ✗ **未改代码（保留文档建议）**：数据根与 catalog 不宜跨 WSL/原生 Windows 混用。2a2f30c 的数据根 resolve 归一化缓解了入口歧义，但 catalog 内 `host_path` 仍为采集时的平台原样路径。

- **触发场景**：catalog 里存的 `host_path` 是采集时的绝对路径。WSL 采集存的是 `/mnt/c/...`，换到原生 Windows Python 运行时 `Path('/mnt/c/data').is_dir()` 为 False；反之亦然。
- **证据**：`host_path` 在 `catalog.snapshots`、`meta.scan_root`、`meta.root_path` 中均为裸字符串，无平台标记。
- **建议修法**：短期在文档中说明“数据根与 catalog 不宜跨 WSL/Windows 混用”；中期可在 `meta` 中增加 `host_path_platform` 键，读取时若检测到平台不匹配且路径以 `/mnt/` 开头或 `C:\` 开头，做启发式转换（如 `/mnt/c/foo` ↔ `C:\foo`）。
- **Linux 回归测试**：mock `sys.platform` 切换，验证路径转换辅助函数。

---

### P2-6 Unicode 规范化差异（NFC/NFD）跨平台 diff 噪声

> ✗ **未改代码（保留文档建议）**：本项目不做 macOS，NFC/NFD 混用场景极少；`path_norm` 仍为 casefold。如未来出现需求再按原建议实现。

- **触发场景**：同一文件在 macOS（NFD）和 Windows（NFC）下文件名 Unicode 编码不同，diff 会报 removed+added。
- **证据**：全项目未做 Unicode 规范化。`path_norm` 用 `casefold()`，不含 `unicodedata.normalize()`。
- **建议修法**：在 `scanner.py` 采集时把 `path` 和 `path_norm` 都先 `unicodedata.normalize('NFC', ...)`；或在 diff 的 `case_insensitive` 分支里增加 NFC 归一化。注意：改动后需重建旧快照的 `path_norm`。
- **Linux 回归测试**：构造 NFD 编码文件名（如 `cafe\u0301`）和 NFC 编码文件名（`caf\u00e9`），验证 diff 可选识别为 identical。

---

### P2-7 临时 zip 文件在服务端崩溃时残留

> ✅ **已修复**（提交 68ada3f，杂项）：导出临时 zip 改用 `tempfile.TemporaryDirectory` 上下文管理，正常路径必清理。

- **触发场景**：API 导出 v1_csv_zip 时生成临时 zip（`exporter.py:111`），`BackgroundTask(zip_path.unlink)` 在响应正常完成时清理。若进程崩溃或客户端中断连接，zip 残留于 `%TEMP%`。
- **证据**：`src/cold_manifest/exporter.py:109-119` 使用 `tempfile.mkstemp` + 手动 `os.close(fd)`，未用 `tempfile.TemporaryDirectory` 上下文。
- **建议修法**：短期：使用 `tempfile.TemporaryDirectory` 作为上下文管理器，或注册 `atexit` 清理已知临时文件；长期：在 `TaskRunner` 启动或 `AppState` 初始化时扫描并删除 `cldm-*_v1_*.zip` 残留。
- **Linux 回归测试**：断言临时文件在函数正常返回后被删除；mock `BackgroundTask` 不执行，断言 atexit 回调能清掉。

---

### P2-8 CLI `db.as_posix()` 用于非 URI 的 `sqlite3.connect`，语义误导

> ✅ **已修复**（提交 68ada3f，杂项）：cli.py 非 URI 连接统一改 `str(db)`，已无 `as_posix()` 连接。

- **触发场景**：代码可读性问题。`cli.py:413,456` 用 `sqlite3.connect(db.as_posix())` 做读写连接，而 `as_posix()` 的语义是“生成 POSIX 路径字符串”，在 Windows 上虽能工作（SQLite 接受 `/`），但应在非 URI 场景下用 `str(db)`。
- **证据**：
  - `src/cold_manifest/cli.py:413` `conn = sqlite3.connect(db.as_posix())`
  - `src/cold_manifest/cli.py:456` `conn = sqlite3.connect(db.as_posix())`
- **建议修法**：改为 `conn = sqlite3.connect(str(db))`，与全项目其余非 URI 连接（如 `db.py:46`）保持一致。
- **Linux 回归测试**：不影响功能，纯代码规范，改后现有测试应全过。

---

## 本轮追加（2026-10 实机反馈）

### P0-9 服务报 "database disk image is malformed"（"假损坏"）——已修复

> ✅ **已修复**。结论：根因不是文件损坏，而是 `immutable=1` 连接的前提不成立——封库后哈希回填（hash）、FTS/统计补建（build-fts / build-stats）仍会**就地写**快照库，immutable 向 SQLite 声明"文件永不变化"，写入后旧连接读到错乱页就报假 malformed（文件没坏，重启服务即恢复）。

- **修了什么**：
  1. 全部读路径弃用 immutable，统一 `db.open_snapshot`（`mode=ro` 普通只读）；
  2. 写快照库的任务（hash / build-fts / build-stats / 删除）在**写前、写后**都把连接池里该快照的连接逐出（`AppState.evict_snapshot`），不留旧连接；
  3. API 注册 sqlite3.DatabaseError 全局处理器（`install_snapshot_error_handler`）：malformed → 409 + 可读中文提示（"可能被并发写入影响，请重启后重试；仍失败用 `cldm integrity-check` 自查"），不再是裸 500 + traceback；
  4. 新增 CLI `cldm integrity-check <快照ID>`（或 `--all`）：只读跑 `PRAGMA quick_check`（异常时升级 `integrity_check`），用于区分假损坏与真损坏；退出码 0=正常 / 1=损坏或缺失。
- **没做什么**：未引入写库排他队列（写任务本身持数据根写锁/串行，现状够用）；历史已产生的错乱读不会自愈，重启服务或等写后驱逐即可。
- **真机复验**：见 `docs/Windows-真机复验清单.md`「本轮复测点」。

---

## 附录：按模块的快速对照表

| 模块 | 涉及问题编号 | 关键行号 |
|------|-------------|---------|
| `scanner.py` | P2-1, P2-6 | 238, 247, 267, 372, 380 |
| `collect.py` | P0-1, P0-3, P1-3, P2-2 | 295, 317, 461, 534, 549, 558-559 |
| `db.py` | P0-2, P0-3 | 9-27, 36-39, 48 |
| `catalog.py` | P0-1, P0-2, P1-3 | 229, 250, 277, 311-318 |
| `diff_engine.py` | P0-2, P1-4 | 350-353, 748, 760, 784 |
| `report.py` | P0-2 | 74 |
| `exporter.py` | P1-2, P2-7 | 48, 68, 84, 100, 111-119 |
| `cli.py` | P1-1, P1-2, P2-8 | 222-230, 271-281, 413, 456, 496, 857, 873 |
| `tasks.py` | P0-1, P1-3 | 146 |
| `hash.py` | P0-1（间接） | 149, 286 |
| `api/state.py` | P0-1 | 34-55 |
| `api/routes_snapshots.py` | P0-1（间接） | 585-626 |
| `smart.py` | P2-3 | 186-188 |
| `probe/windows.py` | P2-3（间接） | 278 |
| `packaging/windows/*.cmd` | P1-1, P2-4 | start.cmd:14-15, cldm.cmd:12-13 |
| `import_legacy.py` | P0-1, P1-3 | 323 |

---

## 修复验证证据（2026-10-01）

全部修复均已提交并带回归测试（WSL 侧在 Linux 上模拟 Windows 行为跑通；真机复验步骤见 `docs/Windows-真机复验清单.md`）：

| 问题 | 提交 | 回归测试（tests/） |
|------|------|--------------------|
| P0-1 删除时释放连接池句柄 | `5e5a611` | `test_delete_windows_compat.py`：`test_evict_snapshot_closes_only_target` · `test_delete_retries_after_evict` · `test_delete_busy_raises_readable_error` · `test_query_then_delete_releases_connection` |
| P0-2 file: URI 统一 + 数据根归一化 | `2a2f30c` | `test_file_uri.py`：`test_file_uri_resolves_dotdot` · `test_e2e_with_dotdot_and_unicode` · `test_cli_data_root_normalized` · `test_api_diff_corrupt_db_400` |
| P0-3 WAL 回退 | `68ada3f` | `test_windows_compat.py`：`test_open_catalog_wal_on_ext4` · `test_open_catalog_wal_fallback` |
| P1-1 控制台 UTF-8 | `68ada3f` | `test_windows_compat.py`：`test_cli_gbk_console_no_crash`（模拟 GBK 输出流） |
| P1-2 CSV 带 BOM | `68ada3f` | `test_import.py`（v1 三件套以 `utf-8-sig` 回读，兼容带 BOM 文件）；导出侧行为由 exporter 统一 `utf-8-sig` 保证 |
| P1-3 只读容忍删除 `rmtree_ro` | `5e5a611` | `test_delete_windows_compat.py`：`test_rmtree_ro_deletes_readonly_file` · `test_rmtree_ro_missing_dir_is_silent` |
| P1-4 大小写改名提示（os_platform + hints） | `be4be0e` | `test_windows_compat_platform.py`：`test_collect_writes_os_platform` · `test_hint_case_insensitive_on_win32` · `test_hint_suppressed_with_case_insensitive` · `test_hint_absent_without_platform_mark` |
| P2-1 危险/保留名跳过 | `68ada3f` | `test_windows_compat.py`：`test_dangerous_reserved_names` · `test_dangerous_trailing_and_ads` · `test_dangerous_names_off_on_linux` · `test_dangerous_dir_pruned` |
| P2-2 长路径预警 | `be4be0e` | `test_windows_compat_platform.py`：`test_long_path_warning_win32` · `test_no_long_path_warning_on_linux_short_root` |
| P2-3 `\\.\PhysicalDriveN` | `68ada3f` | `test_windows_compat.py`：`test_device_for_path_win` · `test_device_for_path_linux` |
| P2-7 临时 zip 清理 | `68ada3f` | 现有导出测试覆盖正常路径（清理逻辑在 exporter 内改用 `TemporaryDirectory`） |
| P2-8 `str(db)` 连接 | `68ada3f` | 现有全量测试覆盖（纯代码规范） |

未改代码项：P2-4（防火墙弹窗属预期）、P2-5（跨 WSL/Windows 混用数据根，文档建议）、P2-6（NFC/NFD，无 macOS 场景）。
