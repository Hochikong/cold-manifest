# cold-manifest

异地冷备磁盘的文件元数据采集与比对工具：扫描冷备盘 → 生成快照 → 浏览/搜索 → 对比两次快照找出新增/变化/丢失（不负责实际数据同步）。CLI 命令名 `cldm`。

**当前状态：P0 已完成；P1.0 历史快照导入已 Web 化**（扫描可导入目录 → 导入任务队列 → 进度 → 浏览）。规划见 `docs/升级方案-v0.3.md` 与 `docs/UX-架构-v0.3.md`；验收证据见 `docs/P0-验收报告.md`；开发环境见 `docs/开发环境与测试.md`。

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

> 同一 data_root 同时只允许一个写者（采集/清扫），并发会提示"data_root 被占用"；中断的大卷采集可用 `cldm collect --resume` 断点续采（详见 docs/Windows-运行说明.md）。


## 查询 API（只读，P0 阶段二）

`cldm serve` 之外可直接用 uvicorn 启动：`CLDM_DATA_ROOT=<数据根> uvicorn cold_manifest.server:app`。
数据根决定 catalog.db 与各快照库位置（默认 `./data`），快照库以 `immutable=1` 只读打开并做 LRU 连接池（上限 16）。

端点统一前缀 `/api`（OpenAPI 见 `/docs`）：

```
GET /api/snapshots                                 # 快照列表（catalog 预计算统计 + volume/disk 概要）
GET /api/snapshots/{sid}                           # 详情（catalog 全行 + meta 键值 + 盘上副本）
GET /api/snapshots/{sid}/stats                     # 扩展名 Top / 大小直方图 / 深度直方图 / top50 大文件
GET /api/snapshots/{sid}/entries?parent_id=&cursor=&limit=&sort=name|size|mtime&order=asc|desc&type=&ext=&min_size=&max_size=&q=
GET /api/snapshots/{sid}/tree?parent_id=           # 单级子目录（带 dir_rollup）+ 直接文件数
GET /api/snapshots/{sid}/du?parent_id=&limit=      # 按大小降序子项（treemap / 排行）
GET /api/snapshots/{sid}/search?q=&mode=prefix     # 前缀搜索（idx_entries_name）
GET /api/snapshots/{sid}/skipped?stage=&warning_type=
GET /api/volumes    GET /api/diffs
```

约定：entries/search/skipped 一律 keyset 游标分页（`limit` ≤ 500，响应 `{items, next_cursor, has_more}`，不返回总数）；`*_ns` 时间戳序列化为字符串防 JS 精度丢失，`*_bytes` 为数字；未知 snapshot_id → 404，参数非法 → 400。

样本 B（2,981,921 文件，原生盘数据根）实测：entries 首页 ~2ms（翻页 ~11ms/页）、tree/du <5ms、stats ~1.4s（覆盖索引聚合）、前缀搜索 ~1ms 热 / 32ms 冷。
