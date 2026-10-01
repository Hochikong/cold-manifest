# cold-manifest

异地冷备磁盘的文件元数据采集与比对工具：扫描冷备盘 → 生成快照 → 浏览/搜索 → 对比两次快照找出新增/变化/丢失（不负责实际数据同步）。CLI 命令名 `cldm`。

**当前状态：P0–P3 已完成，P4 用户反馈项已落地**（采集 + 盘上备份 + 按需哈希 + 查重 + diff 口径/证据 + 全局搜索 + Windows 兼容收尾）。规划见 `docs/升级方案-v0.3.md` 与 `docs/UX-架构-v0.3.md`；Windows 注意事项见 `docs/Windows-兼容性排查.md` 与 `docs/Windows-运行说明.md`；开发环境见 `docs/开发环境与测试.md`。

## 功能一览

- **采集**：单遍 scandir，多分区整盘批次，SSE 进度/取消，断点续采（`--resume`），数据根写锁，采集后自动备份 snapshot.db 到盘上（`_coldmanifest/`）。
- **浏览/搜索**：目录树懒加载、统计画像（直方图/扩展名 Top/treemap）、单快照前缀/全文搜索（FTS5）、**跨快照全局搜索**（顶栏 `Ctrl/Cmd+K`）。
- **对比**：后台物化 diff（8 类变更、目录级汇总、证据等级），口径开关（大小写/时间/尺寸/一致项），Windows 大小写改名提示，路径前缀过滤，HTML 报告 + CSV 导出。
- **按需哈希**：默认关闭；full/sampled 两档、候选指纹、单组精验，`(size, mtime, path)` 缓存；查重三档（文件名/指纹/内容）。
- **副本校验**：`verify-copy` 抽样/全量校验盘上副本。
- **导入/登记**：v1 三件套导入；外部 snapshot.db 就地登记（`import-db`）。
- **运维**：快照删除（含盘上副本）、catalog 重建（预演 + 执行）、FTS/统计补建。
- **Web UI**：7 个路由页（总览/快照/对比/磁盘/任务/搜索/设置）；星标置顶、按卷筛选、哈希面板、HTML 报告分节、右键菜单、服务端排序、卷详情抽屉、设置页数据修复卡片。

## CLI（`cldm`）

```
serve             启动 Web 服务（默认 0.0.0.0:8765）
collect           采集快照（--resume --cross-filesystems --all-partitions --exclude-glob ...）
import-legacy     导入 v1 三件套 CSV
import-db         就地登记外部 snapshot.db（--copy 才拷入数据根）
diff              对比两快照（--hash --case-insensitive --ignore-mtime --ignore-size --show-identical）
export            导出 CSV / v1 同构三件套 zip
report            快照自包含 HTML 报告
hash              按需哈希（--policy full|sampled --candidates --group --limit --root）
duplicates        重复文件报告（--mode content|name|fingerprint）
verify-copy       校验盘上副本（--sample --full）
build-fts         补建 FTS 全文索引
build-stats       补建统计预计算
delete            删除快照（--on-disk keep|delete --force）
rebuild-catalog   重建 catalog（--dry-run 预演）
```

## 开发环境（WSL2）

Python venv 由 uv 管理：`.venv-wsl/`（Python 3.12）。

```bash
# 安装依赖（含 dev）
~/.local/bin/uv pip install -e ".[dev]" --python .venv-wsl/bin/python

# 启动 Web 服务（默认 0.0.0.0:8765）
.venv-wsl/bin/cldm serve

# 启动前端（另开终端；Windows 浏览器访问 http://localhost:5173）
cd frontend && npm run dev

# 运行测试
.venv-wsl/bin/python -m pytest -q
```

> 同一 data_root 同时只允许一个写者（采集/清扫），并发会提示"data_root 被占用"；中断的大卷采集可用 `cldm collect --resume` 断点续采。

## 分发与平台

zip 包解压即用（Windows：`start.cmd` / `cldm.cmd`，已处理 UTF-8 控制台；Linux：启动三件套）。数据根约定：catalog.db 与各快照库都在 `CLDM_DATA_ROOT`（默认 `./data`）下；盘上副本写在各卷根的 `_coldmanifest/`。Windows 注意事项（exFAT/FAT32、长路径、保留名、USB 桥 SMART 等）见 `docs/Windows-兼容性排查.md`。

## 查询 API（只读面摘录）

`cldm serve` 之外也可直接 `CLDM_DATA_ROOT=<数据根> uvicorn cold_manifest.server:app`。快照库以 `immutable=1` 只读打开并做 LRU 连接池（上限 16）。端点统一前缀 `/api`（全量清单见 `docs/升级方案-v0.3.md` 附录 A，OpenAPI 见 `/docs`）：

```
GET  /api/snapshots                        # 快照列表（预计算统计 + pinned）
GET  /api/snapshots/{sid}                  # 详情（meta + 统计 + 盘上副本）
GET  /api/snapshots/{sid}/entries          # 浏览（keyset 游标，type/ext/size/mtime 筛选与排序）
GET  /api/snapshots/{sid}/search           # 单快照搜索（prefix | fulltext）
GET  /api/search                           # 跨快照全局搜索（顶栏 Ctrl/Cmd+K）
POST /api/diffs                            # 发起对比（口径开关；结果物化后分页查询）
GET  /api/snapshots/{sid}/duplicates       # 查重三档
POST /api/snapshots/{sid}/hash             # 按需哈希任务
```

约定：列表/搜索一律 keyset 游标分页（`limit` ≤ 500，响应 `{items, next_cursor, has_more}`，不返回总数）；`*_ns` 时间戳序列化为字符串防 JS 精度丢失，`*_bytes` 为数字；未知 snapshot_id → 404，参数非法 → 400。

样本 B（2,981,921 文件，原生盘数据根）实测：entries 首页 ~2ms（翻页 ~11ms/页）、tree/du <5ms、stats ~1.4s（覆盖索引聚合）、前缀搜索 ~1ms 热 / 32ms 冷。
