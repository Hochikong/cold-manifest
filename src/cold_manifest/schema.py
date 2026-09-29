"""SQLite DDL 常量，与 docs/升级方案-v0.3.md §5.1/§5.2/§5.3 一致。

与文档的差异（有意为之，均为机械性修正）：
- 文档为可读性省略了 IF NOT EXISTS；此处全部补上以保证幂等执行。
- 各表语句重排/拆分为独立语句，不改任何列定义、约束与索引。
"""

# §5.1 快照库 snapshot.db（entries/skipped 见 §4.1）
# 表与索引拆开：导入流程"先建表 → 灌数据 → 封库时建索引"，索引后建写入更快。
SNAPSHOT_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS entries(
  entry_id        INTEGER PRIMARY KEY,
  parent_id       INTEGER NOT NULL,        -- 根为 0
  path            TEXT NOT NULL,           -- 相对路径，'/' 分隔，根为 '.'
  name            TEXT NOT NULL,
  depth           INTEGER NOT NULL,        -- 替代 v1 的 level_1..N
  type            TEXT NOT NULL CHECK (type IN ('file','dir','symlink','other')),
  size_bytes      INTEGER,                 -- 目录为 NULL
  allocated_bytes INTEGER,                 -- 簇对齐后的真实占用
  mtime_ns        INTEGER,
  ctime_ns        INTEGER,
  btime_ns        INTEGER,                 -- 创建时间（NTFS 等支持）
  attrs           INTEGER,                 -- Win 属性位掩码 / POSIX mode
  ext             TEXT,                    -- 小写扩展名（含点），无则 ''
  hash_algo       TEXT,                    -- NULL | 'sha256' | 'blake3'
  hash_hex        TEXT,
  hash_state      TEXT,                    -- 'full' | 'sampled' | 'stale' | NULL
  path_norm       TEXT,                    -- casefold 后路径，跨文件系统比对用
  error           TEXT                     -- 采集该条目的非致命错误
);

CREATE TABLE IF NOT EXISTS skipped(
  path         TEXT NOT NULL,
  warning_type TEXT NOT NULL,
  stage        TEXT NOT NULL,
  detail       TEXT,
  PRIMARY KEY (path, warning_type)
);

CREATE TABLE IF NOT EXISTS dir_rollup(
  entry_id        INTEGER PRIMARY KEY,     -- 仅目录
  file_count      INTEGER NOT NULL,
  dir_count       INTEGER NOT NULL,
  total_bytes     INTEGER NOT NULL,        -- 递归逻辑大小
  total_allocated INTEGER NOT NULL,
  max_mtime_ns    INTEGER                  -- 目录树下最新 mtime
);
"""

SNAPSHOT_INDEXES_DDL = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_entries_path ON entries(path);
CREATE INDEX IF NOT EXISTS idx_entries_parent ON entries(parent_id, name);
CREATE INDEX IF NOT EXISTS idx_entries_ext_size ON entries(ext, size_bytes DESC);
CREATE INDEX IF NOT EXISTS idx_entries_mtime ON entries(mtime_ns);
CREATE INDEX IF NOT EXISTS idx_entries_pathnorm ON entries(path_norm);
CREATE INDEX IF NOT EXISTS idx_entries_hash ON entries(hash_hex) WHERE hash_hex IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_entries_name ON entries(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_entries_size ON entries(size_bytes);
"""

# 兼容既有调用：完整 DDL = 表 + 索引
SNAPSHOT_DDL = SNAPSHOT_TABLES_DDL + SNAPSHOT_INDEXES_DDL

# §5.2 catalog.db（注册表 + 统计缓存）
CATALOG_DDL = """
CREATE TABLE IF NOT EXISTS disks (
  disk_id TEXT PRIMARY KEY,              -- 规范化 serial；无 serial 时 'NOSERIAL_' + 首卷 partition_uuid
  physical_model TEXT, physical_serial TEXT, bridge_model TEXT,
  capacity_bytes INTEGER, interface_type TEXT,
  first_seen TEXT, last_seen TEXT, notes TEXT
);

CREATE TABLE IF NOT EXISTS volumes (
  volume_id TEXT PRIMARY KEY,            -- {serial}_P{index}，兼容 v1
  disk_id TEXT NOT NULL REFERENCES disks,
  partition_index INTEGER,
  partition_uuid TEXT,                   -- 真正的稳定锚
  volume_serial_hex TEXT, filesystem TEXT, label TEXT,
  capacity_bytes INTEGER, notes TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
  snapshot_id TEXT PRIMARY KEY,          -- <volume_id>/<ts>
  volume_id TEXT NOT NULL REFERENCES volumes,
  batch_id TEXT, collected_at TEXT, collector_version TEXT,
  host_path TEXT NOT NULL,
  status TEXT NOT NULL,                  -- sealed | incomplete | importing
  hash_policy TEXT, exclude_rules_json TEXT,
  file_count INTEGER, dir_count INTEGER, total_bytes INTEGER, total_alloc INTEGER,
  zero_byte_count INTEGER, max_depth INTEGER, skipped_count INTEGER,
  smart_summary_json TEXT,               -- health/temp/power_on_hours 摘要，列表页直出
  pinned INTEGER DEFAULT 0, notes TEXT
);

CREATE TABLE IF NOT EXISTS on_disk_copies (
  snapshot_id TEXT PRIMARY KEY REFERENCES snapshots,
  disk_path TEXT, copied_at TEXT, sha256 TEXT, verified_at TEXT,
  status TEXT                            -- ok | skipped_no_space | missing | stale
);

CREATE TABLE IF NOT EXISTS batches (
  batch_id TEXT PRIMARY KEY, disk_id TEXT,
  started_at TEXT, finished_at TEXT,
  status TEXT,                           -- running|done|partial|failed|canceled
  planned_volumes_json TEXT, error TEXT
);

CREATE TABLE IF NOT EXISTS diff_runs (
  diff_id TEXT PRIMARY KEY, a TEXT, b TEXT, options_hash TEXT,
  options_json TEXT, created_at TEXT, duration_ms INTEGER,
  status TEXT, summary_json TEXT, result_path TEXT,
  UNIQUE(a, b, options_hash)             -- 幂等：同参复用既有结果
);

CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY, kind TEXT, payload_json TEXT,
  status TEXT, created_at TEXT, finished_at TEXT, error TEXT,
  related_id TEXT                        -- batch_id 或 diff_id
);

-- 跨快照哈希缓存（§4.5）：同 (size, mtime, path_norm, algo) 直接复用哈希值，
-- 避免跨快照重复读盘。policy 记录产出该哈希的策略（full/sampled），仅作溯源。
CREATE TABLE IF NOT EXISTS hash_cache (
  size_bytes INTEGER NOT NULL,
  mtime_ns   INTEGER NOT NULL,
  path_norm  TEXT NOT NULL,
  algo       TEXT NOT NULL,
  hash_hex   TEXT NOT NULL,
  policy     TEXT,
  created_at TEXT,
  PRIMARY KEY (size_bytes, mtime_ns, path_norm, algo)
);
"""

# §5.3 diff 物化库
DIFF_DDL = """
CREATE TABLE IF NOT EXISTS diff_meta(
  key TEXT PRIMARY KEY,
  value TEXT                             -- a,b,options,duration,summary_json
);

CREATE TABLE IF NOT EXISTS diff_entries(
  id INTEGER PRIMARY KEY,
  change_type TEXT NOT NULL,   -- added/removed/size_changed/mtime_changed/
                               -- content_changed/moved_or_renamed/type_changed/identical
  path TEXT NOT NULL, path_norm TEXT NOT NULL,
  depth INTEGER NOT NULL,
  a_entry_id INTEGER, b_entry_id INTEGER,
  a_size INTEGER, b_size INTEGER,
  a_mtime_ns INTEGER, b_mtime_ns INTEGER,
  a_type TEXT, b_type TEXT               -- 两侧条目类型（file/dir/...），type_changed 展示用
);
CREATE INDEX IF NOT EXISTS idx_de_type_path ON diff_entries(change_type, path);
CREATE INDEX IF NOT EXISTS idx_de_path ON diff_entries(path);
"""
