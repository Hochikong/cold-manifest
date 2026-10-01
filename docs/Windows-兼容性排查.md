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

---

## P0 — 会崩或数据错

### P0-1 删除快照时 `shutil.rmtree` 因文件被占用而失败（Windows 独有）

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

- **触发场景**：Windows NTFS 大小写不敏感。用户在 Windows 上把 `a.txt` 重命名为 `A.TXT`，下次采集后做 diff（默认精确 path），结果会报一行 `removed` + 一行 `added`，而不是 `identical` 或 `moved_or_renamed`。浪费用户注意力。
- **证据**：`src/cold_manifest/diff_engine.py` 默认走 `_materialize_sql`（`path` 精确等值 join），`case_insensitive` 选项默认关闭。
- **建议修法**：在 `canonical_options` 中，若检测到 `sys.platform == "win32"` 且用户未显式指定 `case_insensitive`，可默认设为 `True`；或在 CLI/API 文档中醒目标注 Windows 用户建议加 `--case-insensitive`。更稳妥的做法是在 `materialize_diff` 前置探测：若两侧快照的 `meta.os_platform == 'win32'` 且未显式传 `case_insensitive`，自动开启并记录日志。
- **Linux 回归测试**：构造两个快照，entries.path 分别为 `a/b` 和 `A/B`，验证默认选项在 Linux 下仍报 removed+added（保持现有行为），但在 Windows 下（mock `sys.platform`）自动走 case_insensitive 归并。

---

## P2 — 体验与边缘

### P2-1 未过滤 Windows 保留名 / 末尾点空格 / ADS

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

- **触发场景**：深层目录树（如 node_modules）路径长度超过 Windows `MAX_PATH`（260 字符），`os.scandir`、`open()`、SQLite URI 均可能失败。
- **证据**：全项目未使用 `\\?\` 前缀。Python 3.6+ 在 manifest 中开启 `longPathAware` 后可突破，但项目未提供 manifest。
- **建议修法**：在文档中说明“Windows 用户若需扫描深层目录，请在注册表或组策略中启用长路径支持（Windows 10 1607+）”；代码层面可在 `collect.py` 对 `scan_root` 做检测，若 `len(str(scan_root)) > 200` 给出 warning。
- **Linux 回归测试**：不适用（Linux 路径限制远大于 260）。

---

### P2-3 smartctl Windows 设备路径可能不对（USB 桥）

- **触发场景**：Windows 下 USB 桥接硬盘，smartctl 对盘符（`E:`）常无效，需用 `\\.\PhysicalDriveN`。代码已尝试 `-d sat` 回退，但设备路径本身仍可能是盘符。
- **证据**：`src/cold_manifest/smart.py:186-188` `device_for_path` 在 Windows 下仅返回盘符；`probe/windows.py:278` 在 smartctl 分支里会尝试用 `PhysicalDrive{idx}`，但 `smart.py` 的独立调用（如 `check_smartctl` / CLI 直接调）仍走盘符。
- **建议修法**：`smart.py:device_for_path` 在 Windows 下也尝试通过 `probe/windows.py` 的磁盘索引获取 `PhysicalDriveN`，或至少把 `probe_path_win` 解析出的 disk index 缓存到 catalog/volumes 表，供后续 SMART 查询使用。
- **Linux 回归测试**：mock `probe_path_win` 返回 `disk.index=3`，断言 `device_for_path("E:\\")` 返回 `\\.\PhysicalDrive3`。

---

### P2-4 `start.cmd` 绑定 `0.0.0.0` 触发 Windows 防火墙弹窗

- **触发场景**：首次运行 `start.cmd`，Windows Defender 防火墙弹出“允许 Python 访问网络”提示，对非技术用户造成困惑。
- **证据**：`packaging/windows/start.cmd:15` `".venv-win\Scripts\cldm.exe" serve --host 0.0.0.0 --port 8765`
- **建议修法**：默认改为 `--host 127.0.0.1`，在注释里说明“如需局域网访问，改为 `0.0.0.0`”。或提供 `start-local.cmd` / `start-lan.cmd` 两个入口。
- **Linux 回归测试**：不适用。

---

### P2-5 WSL ↔ 原生 Windows 路径互操作问题（数据可移植性）

- **触发场景**：catalog 里存的 `host_path` 是采集时的绝对路径。WSL 采集存的是 `/mnt/c/...`，换到原生 Windows Python 运行时 `Path('/mnt/c/data').is_dir()` 为 False；反之亦然。
- **证据**：`host_path` 在 `catalog.snapshots`、`meta.scan_root`、`meta.root_path` 中均为裸字符串，无平台标记。
- **建议修法**：短期在文档中说明“数据根与 catalog 不宜跨 WSL/Windows 混用”；中期可在 `meta` 中增加 `host_path_platform` 键，读取时若检测到平台不匹配且路径以 `/mnt/` 开头或 `C:\` 开头，做启发式转换（如 `/mnt/c/foo` ↔ `C:\foo`）。
- **Linux 回归测试**：mock `sys.platform` 切换，验证路径转换辅助函数。

---

### P2-6 Unicode 规范化差异（NFC/NFD）跨平台 diff 噪声

- **触发场景**：同一文件在 macOS（NFD）和 Windows（NFC）下文件名 Unicode 编码不同，diff 会报 removed+added。
- **证据**：全项目未做 Unicode 规范化。`path_norm` 用 `casefold()`，不含 `unicodedata.normalize()`。
- **建议修法**：在 `scanner.py` 采集时把 `path` 和 `path_norm` 都先 `unicodedata.normalize('NFC', ...)`；或在 diff 的 `case_insensitive` 分支里增加 NFC 归一化。注意：改动后需重建旧快照的 `path_norm`。
- **Linux 回归测试**：构造 NFD 编码文件名（如 `cafe\u0301`）和 NFC 编码文件名（`caf\u00e9`），验证 diff 可选识别为 identical。

---

### P2-7 临时 zip 文件在服务端崩溃时残留

- **触发场景**：API 导出 v1_csv_zip 时生成临时 zip（`exporter.py:111`），`BackgroundTask(zip_path.unlink)` 在响应正常完成时清理。若进程崩溃或客户端中断连接，zip 残留于 `%TEMP%`。
- **证据**：`src/cold_manifest/exporter.py:109-119` 使用 `tempfile.mkstemp` + 手动 `os.close(fd)`，未用 `tempfile.TemporaryDirectory` 上下文。
- **建议修法**：短期：使用 `tempfile.TemporaryDirectory` 作为上下文管理器，或注册 `atexit` 清理已知临时文件；长期：在 `TaskRunner` 启动或 `AppState` 初始化时扫描并删除 `cldm-*_v1_*.zip` 残留。
- **Linux 回归测试**：断言临时文件在函数正常返回后被删除；mock `BackgroundTask` 不执行，断言 atexit 回调能清掉。

---

### P2-8 CLI `db.as_posix()` 用于非 URI 的 `sqlite3.connect`，语义误导

- **触发场景**：代码可读性问题。`cli.py:413,456` 用 `sqlite3.connect(db.as_posix())` 做读写连接，而 `as_posix()` 的语义是“生成 POSIX 路径字符串”，在 Windows 上虽能工作（SQLite 接受 `/`），但应在非 URI 场景下用 `str(db)`。
- **证据**：
  - `src/cold_manifest/cli.py:413` `conn = sqlite3.connect(db.as_posix())`
  - `src/cold_manifest/cli.py:456` `conn = sqlite3.connect(db.as_posix())`
- **建议修法**：改为 `conn = sqlite3.connect(str(db))`，与全项目其余非 URI 连接（如 `db.py:46`）保持一致。
- **Linux 回归测试**：不影响功能，纯代码规范，改后现有测试应全过。

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
