import axios from 'axios'

const client = axios.create({
  baseURL: '/api',
  timeout: 60_000,
})

export interface HealthResponse {
  status: string
}

export async function getHealth(): Promise<HealthResponse> {
  const { data } = await client.get<HealthResponse>('/health')
  return data
}

export interface Snapshot {
  snapshot_id: string
  volume_id: string
  batch_id: string
  collected_at: string
  status: string
  hash_policy: string
  file_count: number
  dir_count: number
  total_bytes: number
  total_alloc: number
  zero_byte_count: number
  max_depth: number
  skipped_count: number
  host_path: string
  /** 列表与详情均返回；列表为 bool，详情接口历史上返回 0|1（按真值使用） */
  pinned: boolean
  disk_id: string
  filesystem: string
  label: string | null
  /** 卷昵称 / 盘昵称（label 未设时为 null） */
  volume_nickname: string | null
  disk_nickname: string | null
  volume_capacity_bytes: number
  physical_model: string
  physical_serial: string | null
  disk_capacity_bytes: number
  /** 关联磁盘的身份状态（无关联磁盘 → unknown；旧后端可能缺省） */
  identity_status?: 'verified' | 'unverified' | 'conflict' | 'unknown'
  identity_status_reason?: string | null
}

export interface SnapshotsResponse {
  items: Snapshot[]
  count: number
}

export async function listSnapshots(volume_id?: string): Promise<SnapshotsResponse> {
  const { data } = await client.get<SnapshotsResponse>('/snapshots', {
    params: volume_id ? { volume_id } : undefined,
  })
  return data
}

/** 置顶 / 备注（PATCH /snapshots/{sid}）；两者都缺 → 400，notes ≤ 2000 字符。 */
export interface SnapshotPatchBody {
  pinned?: boolean
  notes?: string
}

/** 快照详情（GET /snapshots/{sid}）：catalog 全行 + volume/disk + 盘上副本 + meta。 */
export interface SnapshotDetail extends Snapshot {
  notes: string | null
  volume: Record<string, unknown>
  on_disk_copy: OnDiskCopy | null
  meta: Record<string, string>
}

/** 更新置顶 / 备注；后端返回更新后的完整详情。 */
export async function patchSnapshot(snapshot_id: string, body: SnapshotPatchBody): Promise<SnapshotDetail> {
  const { data } = await client.patch<SnapshotDetail>(`/snapshots/${encodeURIComponent(snapshot_id)}`, body)
  return data
}

/** 副本校验请求：scope=sample 抽检（默认 200）/ full 全量重算。 */
export interface VerifyCopyBody {
  scope: 'sample' | 'full'
  sample_size?: number
  seed?: number
}

export interface VerifyCopySample {
  path: string
  expected: string | null
  actual: string | null
  status: 'match' | 'mismatch' | 'missing' | 'unreadable'
}

export interface VerifyCopyReport {
  snapshot_id: string
  host_db: string
  disk_db: string
  copy: {
    /** ok | mismatch | missing_disk | not_recorded */
    status: string
    recorded_sha256: string | null
    disk_sha256: string | null
    host_sha256: string | null
    /** unchanged | modified_since_collection */
    host_status: string
  }
  sidecar: {
    /** ok | missing | problems */
    status: string
    problems: string[]
  }
  source: {
    available: boolean
    checked: number
    match: number
    mismatch: number
    missing: number
    unreadable: number
    /** available=false 时的原因 */
    error?: string
    samples: VerifyCopySample[]
  }
  ok: boolean
}

/** 同步校验盘上副本（+ 可选源文件抽检），返回与 CLI verify-copy 同源的报告。 */
export async function verifySnapshotCopy(snapshot_id: string, body: VerifyCopyBody): Promise<VerifyCopyReport> {
  const { data } = await client.post<VerifyCopyReport>(
    `/snapshots/${encodeURIComponent(snapshot_id)}/verify-copy`,
    body,
  )
  return data
}

/** build-fts / build-stats 提交返回：{ task_id, status }，重复提交 409。 */
export type BuildIndexKind = 'build_fts' | 'build_stats'

export async function submitBuildIndexTask(snapshot_id: string, kind: BuildIndexKind): Promise<HashTaskResponse> {
  const suffix = kind === 'build_fts' ? 'build-fts' : 'build-stats'
  const { data } = await client.post<HashTaskResponse>(`/snapshots/${encodeURIComponent(snapshot_id)}/${suffix}`)
  return data
}

/** 盘上副本登记（catalog.on_disk_copies 行）。 */
export interface OnDiskCopy {
  snapshot_id: string
  disk_path: string | null
  copied_at: string | null
  sha256: string | null
  verified_at: string | null
  /** ok | skipped_no_space | missing | stale */
  status: string
}

export async function getSnapshot(snapshot_id: string): Promise<SnapshotDetail> {
  const { data } = await client.get<SnapshotDetail>(`/snapshots/${encodeURIComponent(snapshot_id)}`)
  return data
}

export interface ExtTopItem {
  ext: string
  total_bytes?: number
  count?: number
}

export interface SizeHistogramItem {
  label: string
  count: number
  total_bytes: number
}

export interface DepthHistogramItem {
  depth: number
  count: number
}

export interface TopFileItem {
  entry_id: number
  path: string
  name: string
  size_bytes: number
  mtime_ns: string | null
}

export interface SnapshotStats {
  snapshot_id: string
  ext_top_by_bytes: ExtTopItem[]
  ext_top_by_count: ExtTopItem[]
  size_histogram: SizeHistogramItem[]
  depth_histogram: DepthHistogramItem[]
  zero_byte_count: number
  top_files: TopFileItem[]
}

export async function getSnapshotStats(snapshot_id: string): Promise<SnapshotStats> {
  const { data } = await client.get(`/snapshots/${encodeURIComponent(snapshot_id)}/stats`)
  return data
}

export interface DirRollup {
  file_count: number
  dir_count: number
  total_bytes: number
  total_allocated: number
  max_mtime_ns: string | null
}

export interface Entry {
  entry_id: number
  name: string
  type: 'file' | 'dir' | 'symlink' | 'other'
  size_bytes: number | null
  allocated_bytes: number | null
  mtime_ns: string | null
  ext: string
  hash_state: string | null
  attrs: string | null
  rollup?: DirRollup
}

export interface EntriesResponse {
  snapshot_id: string
  parent_id: number
  parent_path: string | null
  direct_child_counts: { files: number; dirs: number; symlinks: number; others: number }
  items: Entry[]
  next_cursor: string | null
  has_more: boolean
}

export interface ListEntriesParams {
  parent_id?: number
  cursor?: string
  limit?: number
  sort?: 'name' | 'size' | 'mtime'
  order?: 'asc' | 'desc'
  type?: 'file' | 'dir' | 'symlink' | 'other'
  ext?: string
  min_size?: number
  max_size?: number
  q?: string
}

export async function listEntries(snapshot_id: string, params: ListEntriesParams = {}): Promise<EntriesResponse> {
  const { data } = await client.get(`/snapshots/${encodeURIComponent(snapshot_id)}/entries`, { params })
  return data
}

export interface TreeDir {
  entry_id: number
  name: string
  file_count: number
  dir_count: number
  total_bytes: number
  total_allocated: number
  max_mtime_ns: string | null
}

export interface TreeResponse {
  snapshot_id: string
  parent_id: number
  parent_path: string | null
  dirs: TreeDir[]
  direct_file_count: number
}

export async function getTree(snapshot_id: string, parent_id = 0): Promise<TreeResponse> {
  const { data } = await client.get(`/snapshots/${encodeURIComponent(snapshot_id)}/tree`, { params: { parent_id } })
  return data
}

export interface DuItem {
  entry_id: number
  name: string
  type: 'file' | 'dir'
  path: string
  total_bytes: number
}

export interface DuResponse {
  snapshot_id: string
  parent_id: number
  items: DuItem[]
}

export async function getDu(snapshot_id: string, parent_id = 0, limit = 50): Promise<DuResponse> {
  const { data } = await client.get(`/snapshots/${encodeURIComponent(snapshot_id)}/du`, { params: { parent_id, limit } })
  return data
}

export interface SearchItem {
  entry_id: number
  name: string
  path: string
  type: 'file' | 'dir' | 'symlink' | 'other'
  size_bytes: number | null
  ext: string
}

export interface SearchResponse {
  snapshot_id: string
  mode: string
  fulltext_available?: boolean
  items: SearchItem[]
  next_cursor: string | null
  has_more: boolean
}

export interface SearchParams {
  q: string
  mode?: 'prefix' | 'fulltext'
  type?: 'file' | 'dir' | 'symlink' | 'other'
  ext?: string
  min_size?: number
  max_size?: number
  cursor?: string
  limit?: number
}

export async function searchEntries(snapshot_id: string, params: SearchParams): Promise<SearchResponse> {
  const { data } = await client.get(`/snapshots/${encodeURIComponent(snapshot_id)}/search`, { params })
  return data
}

export interface Volume {
  volume_id: string
  disk_id: string
  partition_index: number
  partition_uuid: string | null
  volume_serial_hex: string | null
  filesystem: string
  label: string | null
  /** 用户昵称（PATCH /volumes/{id} 设置） */
  nickname: string | null
  /** 所属磁盘昵称 */
  disk_nickname: string | null
  capacity_bytes: number
  physical_model: string
  physical_serial: string | null
  disk_capacity_bytes: number
  snapshot_count: number
}

export interface VolumesResponse {
  items: Volume[]
  count: number
}

export async function listVolumes(): Promise<VolumesResponse> {
  const { data } = await client.get('/volumes')
  return data
}

/** ATA/SATA SMART 属性表单行（契约冻结；NVMe 盘 ata_attributes 为 []）。 */
export interface AtaAttribute {
  id: number
  name: string
  value: number
  worst: number
  thresh: number
  raw_value: string
  raw_string: string | null
  when_failed: string | null
  /** 中文名（后端逐步落地；旧后端缺字段 / 无翻译 → undefined 或空串，前端回退 name） */
  name_zh?: string | null
  /** when_failed 的中文（可选，兜底用 when_failed 原值映射） */
  when_failed_zh?: string | null
}

/**
 * ATA 机械盘扩展指标 / 身份与链路信息（契约冻结）。
 * 后端缺项 → null；前端缺项显示 —，绝不渲染 "null"。
 */
export interface AtaExtras {
  ata_attributes?: AtaAttribute[] | null
  power_cycle_count?: number | null
  load_cycle_count?: number | null
  udma_crc_errors?: number | null
  raw_read_error_rate?: number | null
  seek_error_rate?: number | null
  spin_retry_count?: number | null
  power_off_retract_count?: number | null
  airflow_temperature_c?: number | null
  head_flying_hours?: number | null
  interface_speed_current?: string | null
  interface_speed_max?: string | null
  sata_version?: string | null
  ata_version?: string | null
  trim?: boolean | null
  zoned?: string | null
  model_family?: string | null
  rotation_rate?: number | null
  form_factor?: string | null
}

export interface LatestSmart {
  health: string
  temperature_c: number | null
  power_on_hours: number | null
  reallocated_ct: number | null
  pending_ct: number | null
  collected_at: string
}

/** SSD 专属指标（契约冻结，字段名勿改；后端未实现/旧快照 → ssd 为 null/缺省）。 */
export interface SsdMetrics {
  /** 剩余寿命 %（NVMe = 100 - percentage_used；SATA 来自 202/231/233 等） */
  life_left_pct: number | null
  /** NVMe 已用寿命 % */
  percentage_used: number | null
  available_spare_pct: number | null
  /** 累计写入字节 */
  written_bytes: number | null
  read_bytes: number | null
  /** 人类可读 TB（后端已换算，前端优先用它） */
  written_tb: number | null
  read_tb: number | null
  media_errors: number | null
  unsafe_shutdowns: number | null
  power_cycles: number | null
  /** 控制器忙时（分钟） */
  controller_busy_minutes: number | null
  /** NVMe 多温度探头 */
  temp_sensors: number[]
  source: 'nvme' | 'ata'
}

export interface Disk {
  disk_id: string
  physical_model: string | null
  physical_serial: string | null
  bridge_model: string | null
  /** 用户昵称（PATCH /disks/{id} 设置） */
  nickname: string | null
  capacity_bytes: number
  interface_type: string | null
  first_seen: string
  last_seen: string
  volume_count: number
  snapshot_count: number
  latest_smart: LatestSmart | null
  /** 磁盘身份状态（序列号核对结论：verified/unverified/conflict/unknown） */
  identity_status: 'verified' | 'unverified' | 'conflict' | 'unknown'
  /** 中文一句话原因；无原因时为 null/缺省 */
  identity_status_reason: string | null
  /** 身份验证来源：auto=采集时自动核对 / manual=用户手动确认；旧后端缺省 */
  identity_verified_source?: 'auto' | 'manual' | null
  /** 身份验证时间（ISO 字符串）；旧后端缺省 */
  identity_verified_at?: string | null
}

export interface DisksResponse {
  items: Disk[]
  count: number
}

export async function listDisks(): Promise<DisksResponse> {
  const { data } = await client.get('/disks')
  return data
}

export interface DiskVolume {
  volume_id: string
  disk_id: string
  partition_index: number | null
  partition_uuid: string | null
  volume_serial_hex: string | null
  filesystem: string
  label: string | null
  nickname: string | null
  capacity_bytes: number
  notes: string | null
}

export interface DiskSnapshot {
  snapshot_id: string
  volume_id: string
  collected_at: string
  status: string
  file_count: number
  dir_count: number
  total_bytes: number
  skipped_count: number
}

export interface DiskDetail extends Disk {
  notes: string | null
  volumes: DiskVolume[]
  snapshots: DiskSnapshot[]
  /** 详情接口返回 disk_smart 全行（含 raw_json/source），比列表摘要更全 */
  latest_smart: LatestSmartFull | null
  /** 最新一个带 meta.smart_error 的快照（解释「为什么没有 SMART」） */
  smart_error: SmartErrorInfo | null
}

export async function getDisk(disk_id: string): Promise<DiskDetail> {
  const { data } = await client.get(`/disks/${encodeURIComponent(disk_id)}`)
  return data
}

/** 设置/清除磁盘昵称（PATCH /disks/{id}）；空串 = 清除，≤64 字符，未知 id → 404。 */
export async function patchDiskNickname(disk_id: string, nickname: string): Promise<{ disk_id: string; nickname: string | null }> {
  const { data } = await client.patch(`/disks/${encodeURIComponent(disk_id)}`, { nickname })
  return data
}

/**
 * PATCH /disks/{id} 扩展字段（后端逐项落地，旧后端可能返回 400/422，由调用方按 detail 提示）：
 * - identity_verified：手动确认/撤销身份（后端记 identity_verified_source="manual" + 时间）；
 * - physical_serial：真盘序列号直接修改；
 * - disk_serial：磁盘 ID 来源，仅该盘没有快照时可改，有快照 → 400（detail 为人话说明）。
 */
export interface DiskPatchBody {
  nickname?: string
  identity_verified?: boolean
  physical_serial?: string
  disk_serial?: string
}

/** 通用磁盘字段更新（昵称 / 身份确认 / 序列号），返回更新后的完整详情。 */
export async function patchDisk(disk_id: string, body: DiskPatchBody): Promise<DiskDetail> {
  const { data } = await client.patch<DiskDetail>(`/disks/${encodeURIComponent(disk_id)}`, body)
  return data
}

/** POST /disks/{id}/identity/recheck 返回：现场重新校验身份的结论。 */
export interface IdentityRecheckResult {
  ok: boolean
  /** verified=现场核对通过 / unverified=核对未通过 / not_attached=盘当前不在线 */
  verdict: 'verified' | 'unverified' | 'not_attached'
  reason: string
  identity_serial: string | null
  capacity_bytes: number | null
}

/** 现场重新校验磁盘身份；盘不在线是 200 + not_attached（不抛错），未知盘才 404。 */
export async function recheckDiskIdentity(disk_id: string): Promise<IdentityRecheckResult> {
  const { data } = await client.post<IdentityRecheckResult>(`/disks/${encodeURIComponent(disk_id)}/identity/recheck`)
  return data
}

/** 设置/清除卷昵称（PATCH /volumes/{id}）；空串 = 清除，非法 volume_id → 400，未知 → 404。 */
export async function patchVolumeNickname(volume_id: string, nickname: string): Promise<{ volume_id: string; nickname: string | null }> {
  const { data } = await client.patch(`/volumes/${encodeURIComponent(volume_id)}`, { nickname })
  return data
}

export interface SmartItem extends AtaExtras {
  disk_id: string
  snapshot_id: string
  collected_at: string
  health: string
  temperature_c: number | null
  power_on_hours: number | null
  reallocated_ct: number | null
  pending_ct: number | null
  start_stop_ct: number | null
  spin_up_ms: number | null
  device_type: string | null
  raw_json: string | null
  source: string
  ssd?: SsdMetrics | null
}

export interface SmartHistoryResponse {
  disk_id: string
  items: SmartItem[]
  count: number
}

export async function getDiskSmartHistory(disk_id: string): Promise<SmartHistoryResponse> {
  const { data } = await client.get(`/disks/${encodeURIComponent(disk_id)}/smart`)
  return data
}

/** 盘详情里的 smart_error（最新一个带 meta.smart_error 的快照）：解释「为什么没有 SMART」。 */
export interface SmartErrorInfo {
  smart_error: string
  smart_error_raw: string | null
  snapshot_id: string
}

/** 盘详情返回的 latest_smart：disk_smart 全行 + source（catalog=历史表，meta=老快照 meta 现场解析）。 */
export interface LatestSmartFull extends LatestSmart, AtaExtras {
  start_stop_ct: number | null
  spin_up_ms: number | null
  device_type: string | null
  raw_json: string | null
  snapshot_id?: string
  source?: string
  ssd?: SsdMetrics | null
}

/** POST /disks/{id}/smart/read 单次尝试记录。 */
export interface SmartReadAttempt {
  device_type: string
  rc: number | null
  error: string | null
  stderr_excerpt: string
  /** 诊断补充字段（后端 read_smart_verbose 返回，旧响应可能缺） */
  device?: string
  argv?: string
  exit_status?: number | null
  stdout_messages?: string
  ok?: boolean
}

/** 现场读取的映射结论：smartctl --scan 扫描表 / 候选链 / 生效设备 / 容量软校验。 */
export interface SmartScanInfo {
  devices?: { device: string; type: string }[]
  candidates?: { device: string; type: string; source: string }[]
  device_used?: string | null
  mapped_from_scan?: boolean
  /** 容量软校验结果：null=通过/未校验，"mismatch"=扫描映射疑似错盘被剔除 */
  capacity_check?: string | null
}

/** parse_smart 输出（含真盘身份字段 model/serial/firmware）。 */
export interface ParsedSmart extends AtaExtras {
  health: string
  temperature_c: number | null
  power_on_hours: number | null
  reallocated_ct: number | null
  pending_ct: number | null
  start_stop_ct: number | null
  spin_up_ms: number | null
  device_type: string | null
  model: string | null
  serial: string | null
  firmware: string | null
  ssd?: SsdMetrics | null
}

/** 现场读取 SMART 返回（不写库）。ok=false 时 message/attempts/raw_excerpt 给出人话原因。 */
export interface SmartReadResult {
  disk_id: string
  device: string
  ok: boolean
  device_type: string
  reason: string | null
  message: string | null
  raw_excerpt: string
  attempts: SmartReadAttempt[]
  parsed: ParsedSmart | null
  exit_status?: number | null
  device_candidates?: string[] | null
  scan_info?: SmartScanInfo | null
}

/** 对当前插着的盘现场读一次 SMART；盘不在线时后端 404（由调用方处理）。 */
export async function readDiskSmartNow(disk_id: string): Promise<SmartReadResult> {
  const { data } = await client.post(`/disks/${encodeURIComponent(disk_id)}/smart/read`, {})
  return data
}

/** 真盘身份（来自 smartctl：-i 段）。 */
export interface SmartIdentity {
  model: string | null
  serial: string | null
  firmware: string | null
  capacity_bytes: number | null
}

/** 从 smartctl 原始 JSON 文本解析真盘身份；坏 JSON/全空返回 null（前端本地解析，不发请求）。 */
export function parseSmartIdentity(raw_json: string | null | undefined): SmartIdentity | null {
  if (!raw_json) return null
  try {
    const j = JSON.parse(raw_json) as Record<string, unknown>
    if (!j || typeof j !== 'object') return null
    const model = (j.model_name as string | undefined) || (j.device_model as string | undefined) || null
    const serial = ((j.serial_number as string | undefined) || '').trim() || null
    const firmware = (j.firmware_version as string | undefined) || null
    const cap = (j.user_capacity as { bytes?: number } | undefined)?.bytes
    const capacity_bytes = typeof cap === 'number' ? cap : null
    if (!model && !serial && !firmware && capacity_bytes == null) return null
    return { model, serial, firmware, capacity_bytes }
  } catch {
    return null
  }
}

export interface PreflightBody {
  path: string
}

export interface PreflightResponse {
  path: string
  writable: boolean
  smartctl_available: boolean
  smartctl_path: string | null
  device_type_hint: string
  is_smart_capable: boolean
  warnings: string[]
  /** 仅当"ATA 直通读不到真盘序列号、也拿不到可用的系统序列号"时才为 true（后端缺失字段时按 false 处理，不拦） */
  requires_manual_serial?: boolean
  /** 需要手填序列号的人话原因 */
  manual_serial_reason?: string | null
  /** ATA 直通读到的真盘序列号（可作为身份依据） */
  smart_serial?: string | null
  /** 是否拿到了可用的序列号（真盘序列号或系统序列号） */
  serial_usable?: boolean
  /** 系统枚举层（Get-Disk/WMI）上报的 ID：可能是盒子 ID，不能单独作身份依据，仅作对照展示 */
  probe_serial?: string | null
  /** USB 桥/盒子型号 */
  bridge_model?: string | null
  /** 接口类型（如 usb/jmicron bridge） */
  interface_type?: string | null
}

export async function collectPreflight(body: PreflightBody): Promise<PreflightResponse> {
  const { data } = await client.post('/collect/preflight', body)
  return data
}

export interface DeleteSnapshotResponse {
  snapshot_id: string
  deleted_host: boolean
  deleted_disk: boolean | null
  freed_bytes: number
  diffs_removed: string[]
  warnings: string[]
}

export interface DeleteSnapshotBlocked {
  message: string
  diffs: string[]
  tasks: string[]
}

export async function deleteSnapshot(
  snapshot_id: string,
  params: { on_disk: 'keep' | 'delete'; force: boolean }
): Promise<DeleteSnapshotResponse> {
  const { data } = await client.delete(`/snapshots/${encodeURIComponent(snapshot_id)}`, { params })
  return data
}

/** 删除磁盘 / 卷的登记记录（不做级联：其下仍有快照时后端返回 409）。 */
export interface DeleteRegistryResponse {
  disk_id?: string
  volume_id?: string
  deleted_volumes?: number
  deleted_snapshots: number
}

/** 409 响应体形如 {"detail": {message, snapshots, diffs}}；detail 为嵌套对象。 */
export interface DeleteRegistryBlocked {
  message: string
  snapshots: string[]
  diffs: string[]
}

export async function deleteDisk(disk_id: string): Promise<DeleteRegistryResponse> {
  const { data } = await client.delete(`/disks/${encodeURIComponent(disk_id)}`)
  return data
}

export async function deleteVolume(volume_id: string): Promise<DeleteRegistryResponse> {
  const { data } = await client.delete(`/volumes/${encodeURIComponent(volume_id)}`)
  return data
}

export interface VolumeTrendItem {
  snapshot_id: string
  collect_time: string
  status: string
  file_count: number
  dir_count: number
  total_bytes: number
  max_depth: number
}

export interface VolumeTrendsResponse {
  volume_id: string
  items: VolumeTrendItem[]
}

export async function getVolumeTrends(volume_id: string, limit?: number): Promise<VolumeTrendsResponse> {
  const { data } = await client.get(`/volumes/${encodeURIComponent(volume_id)}/trends`, {
    params: limit ? { limit } : undefined,
  })
  return data
}

export type DuplicateMode = 'content' | 'name' | 'fingerprint'

export interface DuplicateItem {
  /** content/fingerprint 档：组哈希；name 档无此字段 */
  hash_hex?: string
  /** name 档：组内文件名（MIN(name) 二进制序） */
  name?: string
  size_bytes: number
  count: number
  /** name 档为 null（无法得知真实浪费） */
  wasted_bytes: number | null
  /** fingerprint 档：组内是否全部为完整哈希 */
  verified?: boolean
  paths: string[]
  paths_truncated: boolean
}

export interface DuplicatesResponse {
  snapshot_id: string
  mode: DuplicateMode
  hash_algo: string | null
  min_size: number
  hashed_files: number
  duplicate_groups: number
  total_wasted_bytes: number | null
  items: DuplicateItem[]
  has_more: boolean
  next_cursor: string | null
}

export async function getDuplicates(
  snapshot_id: string,
  params?: { mode?: DuplicateMode; min_size?: number; limit?: number; cursor?: string }
): Promise<DuplicatesResponse> {
  const { data } = await client.get(`/snapshots/${encodeURIComponent(snapshot_id)}/duplicates`, {
    params,
  })
  return data
}

export interface HashTaskBody {
  algo?: 'sha256'
  policy: 'full' | 'sampled'
  root?: string
  scope?: 'incremental' | 'candidates'
  /** 指定 hash_hex（可只给前缀）：只对该组做完整哈希精验（与 scope=candidates 互斥） */
  group?: string
}

export interface HashTaskResponse {
  task_id: string
  status: string
}

/** 哈希任务 result 的已知字段（后端 run_hash_task 返回；字段缺失时不展示对应项）。 */
export interface HashTaskResult {
  snapshot_id?: string
  algo?: string
  policy?: string
  scope?: 'incremental' | 'candidates' | 'group'
  total?: number
  computed?: number
  cached?: number
  errors?: number
  bytes_hashed?: number
  elapsed_s?: number
  host_path?: string
}

/** 提交按需哈希任务（scope=candidates 只算大小重复的候选文件，通常秒级~分钟级）。 */
export async function submitHashTask(snapshot_id: string, body: HashTaskBody): Promise<HashTaskResponse> {
  const { data } = await client.post(`/snapshots/${encodeURIComponent(snapshot_id)}/hash`, body)
  return data
}

export type EvidenceLevel = 'hash' | 'size+mtime'

export interface DiffEvidence {
  content_changed: 'hash' | 'unavailable'
  moved_or_renamed: 'hash' | 'unavailable'
  size_changed: 'size' | 'unavailable'
  mtime_changed: 'mtime' | 'unavailable'
  type_changed: 'type' | 'unavailable'
  identical: 'hash' | 'size+mtime'
  [key: string]: string
}

export interface DiffRun {
  diff_id: string
  a: string
  b: string
  /** A/B 快照的人类可读标签（卷昵称 · 时间戳目录），可能为空串；后端未上线时缺失 */
  labels?: { a: string; b: string }
  options_hash: string
  created_at: string
  duration_ms: number
  status: string
  result_path: string
  options: { hash: string; case_insensitive?: boolean; ignore_mtime?: boolean; ignore_size?: boolean; show_identical?: boolean }
  summary: Record<string, number | null>
  evidence_level?: EvidenceLevel
  evidence?: DiffEvidence
}

export interface DiffsResponse {
  items: DiffRun[]
  has_more: boolean
  next_cursor: string | null
}

export interface ListDiffsParams {
  limit?: number
  cursor?: string
}

export async function listDiffs(params: ListDiffsParams = {}): Promise<DiffsResponse> {
  const { data } = await client.get('/diffs', { params })
  return data
}

export interface DiffCreateBody {
  a: string
  b: string
  options?: {
    hash: 'none' | 'sha256'
    case_insensitive?: boolean
    ignore_mtime?: boolean
    ignore_size?: boolean
    show_identical?: boolean
  }
}

export interface DiffCreateResponse {
  diff_id: string
  status: string
  counts: Record<string, number | null>
  reused: boolean
  elapsed_s: number
  evidence_level?: EvidenceLevel
  evidence?: DiffEvidence
}

export async function createDiff(body: DiffCreateBody): Promise<DiffCreateResponse> {
  const { data } = await client.post('/diffs', body)
  return data
}

export async function getDiff(diff_id: string): Promise<DiffRun> {
  const { data } = await client.get(`/diffs/${diff_id}`)
  return data
}

export interface DiffDeleteResponse {
  diff_id: string
  deleted: boolean
}

/** 删除对比结果（物化库 + catalog 行）；不影响两侧快照。running → 409。 */
export async function deleteDiff(diff_id: string): Promise<DiffDeleteResponse> {
  const { data } = await client.delete(`/diffs/${diff_id}`)
  return data
}

export interface DiffSummary {
  diff_id: string
  total_changes: number
  total_size_delta: number
  by_parent_dir: { parent_dir: string; count: number; size_delta: number }[]
}

export async function getDiffSummary(diff_id: string, top = 50): Promise<DiffSummary> {
  const { data } = await client.get(`/diffs/${diff_id}/summary`, { params: { top } })
  return data
}

export interface DiffEntry {
  id: number
  category: string
  path: string
  depth: number
  a_type: string | null
  b_type: string | null
  a_size: number | null
  b_size: number | null
  a_mtime_ns: string | null
  b_mtime_ns: string | null
  size_delta: number | null
  b_path?: string | null
}

export interface DiffEntriesResponse {
  diff_id: string
  items: DiffEntry[]
  next_cursor: string | null
  has_more: boolean
}

export interface DiffEntriesParams {
  category?: string
  path_prefix?: string
  cursor?: string
  limit?: number
  sort?: 'path' | 'size_delta'
  order?: 'asc' | 'desc'
}

export async function listDiffEntries(diff_id: string, params: DiffEntriesParams = {}): Promise<DiffEntriesResponse> {
  const { data } = await client.get(`/diffs/${diff_id}/entries`, { params })
  return data
}

export function exportSnapshotUrl(snapshot_id: string, format: 'csv' | 'v1_csv' = 'csv'): string {
  return `/api/snapshots/${encodeURIComponent(snapshot_id)}/export?format=${format}`
}

/** 快照自包含 HTML 报告（GET /report?format=html），可按节过滤（默认全节）。 */
export const REPORT_SECTIONS = [
  { value: 'overview', label: '总览' },
  { value: 'extensions', label: '扩展名' },
  { value: 'sizes', label: '大小分布' },
  { value: 'depth', label: '深度' },
  { value: 'topdirs', label: '顶层目录' },
  { value: 'skipped', label: '跳过项' },
] as const

export function snapshotReportUrl(snapshot_id: string, sections?: readonly string[]): string {
  const base = `/api/snapshots/${encodeURIComponent(snapshot_id)}/report?format=html`
  if (!sections || sections.length === 0) return base
  return `${base}&sections=${sections.map(encodeURIComponent).join(',')}`
}

export interface SkippedItem {
  path: string
  warning_type: string
  stage: string
  detail: string | null
}

export interface SkippedResponse {
  snapshot_id: string
  items: SkippedItem[]
  next_cursor: string | null
  has_more: boolean
}

export interface SkippedParams {
  cursor?: string
  limit?: number
  stage?: string
  warning_type?: string
}

/** 未采集项（skipped 表），keyset 分页，支持 stage / warning_type 过滤。 */
export async function getSkipped(snapshot_id: string, params: SkippedParams = {}): Promise<SkippedResponse> {
  const { data } = await client.get(`/snapshots/${encodeURIComponent(snapshot_id)}/skipped`, { params })
  return data
}

export interface AttachedVolume {
  path: string
  device: string
  filesystem: string
  label: string
}

export interface AttachedDisk {
  device: string
  model: string
  /** 序列号主显值：优先 ATA 直读（smartctl），读不到时为系统枚举值 */
  serial: string
  size_bytes: number | null
  /** serial 来源："smartctl"=ATA 直读命中；"wmi_verified"/"storage"=系统枚举回退；缺失=旧后端 */
  serial_source?: string
  /** true=ATA 直读（可信）；false=系统枚举回退（未核实）；缺失=旧后端 */
  serial_verified?: boolean
  /** 系统枚举值：仅当它与 serial 不同时给出（可能是盒子 ID）；缺失=旧后端 */
  system_serial?: string | null
  /** 系统枚举到的未核对原始值（可能是盒子占位号）；既有语义保留 */
  serial_unverified_raw?: string | null
  volumes: AttachedVolume[]
}

export interface AttachedDisksResponse {
  available: boolean
  reason?: string
  items: AttachedDisk[]
  count?: number
}

/** 本机可见盘（lsblk / WMI 枚举）；不可用时 available=false + reason，不抛错。 */
export async function getAttachedDisks(): Promise<AttachedDisksResponse> {
  const { data } = await client.get('/disks/attached')
  return data
}

export function exportDiffUrl(diff_id: string): string {
  return `/api/diffs/${diff_id}/export?format=csv`
}

export function diffReportUrl(diff_id: string, format: 'html' = 'html'): string {
  return `/api/diffs/${diff_id}/report?format=${format}`
}

export interface ImportCandidate {
  snapshot_dir: string
  volume_id: string
  collect_time: string
  tree_csv_bytes: number
  status: 'new' | 'imported'
}

export interface ImportScanResponse {
  root: string
  candidates: ImportCandidate[]
  errors: string[]
}

export async function scanImports(path: string): Promise<ImportScanResponse> {
  const { data } = await client.get('/imports/scan', { params: { path } })
  return data
}

export interface ImportCreateBody {
  snapshot_dir: string
  volume_id?: string | null
  force?: boolean
}

export interface ImportCreateResponse {
  task_id: string
  status: string
}

export async function createImport(body: ImportCreateBody): Promise<ImportCreateResponse> {
  const { data } = await client.post('/imports', body)
  return data
}

// ---------------------------------------------------------------- 就地登记外部 snapshot.db（POST /imports/db）

export interface ImportDbResponse {
  /** false = 幂等命中（该库此前已登记，本次只返回现有登记） */
  created: boolean
  snapshot_id: string
  volume_id: string
  db_path: string
  snapshot: Record<string, unknown> | null
  warnings: string[]
}

/** 就地登记已封库的 snapshot.db（只引用原文件，不拷入数据根）。 */
export async function importDb(path: string): Promise<ImportDbResponse> {
  const { data } = await client.post<ImportDbResponse>('/imports/db', { path })
  return data
}

// ---------------------------------------------------------------- catalog 重建（POST /admin/rebuild-catalog）

export interface RebuildCatalogSummary {
  scanned: number
  disks_added: number
  volumes_added: number
  snapshots_added: number
  copies_added: number
  fields_backfilled: number
  snapshots_skipped: number
  warnings: string[]
  dry_run: boolean
}

/** 重建/回填 catalog 注册行；dry_run=true 只输出计划不写。写操作持数据根锁（被占 → 409）。 */
export async function rebuildCatalog(dry_run: boolean): Promise<RebuildCatalogSummary> {
  const { data } = await client.post<RebuildCatalogSummary>('/admin/rebuild-catalog', { dry_run })
  return data
}

/** 从 axios 错误里取后端 detail：字符串优先；{detail:{message}} 取 message；兜底 error.message。 */
export function apiErrorDetail(e: unknown): string {
  if (axios.isAxiosError(e)) {
    const data: unknown = e.response?.data
    if (typeof data === 'string' && data) return data
    if (data && typeof data === 'object' && 'detail' in data) {
      const d = (data as { detail?: unknown }).detail
      if (typeof d === 'string' && d) return d
      if (d && typeof d === 'object') {
        const m = (d as { message?: unknown }).message
        if (typeof m === 'string' && m) return m
        return JSON.stringify(d)
      }
    }
  }
  return e instanceof Error ? e.message : String(e)
}

export interface CollectCreateBody {
  path: string
  volume_id?: string | null
  serial?: string | null
  exclude_globs?: string[]
  exclude_hidden?: boolean
  include_system?: boolean
  smartctl?: boolean
  on_disk_copy?: boolean
  all_partitions?: boolean
  resume?: boolean
  cross_filesystems?: boolean
}

export interface SingleCollectCreateResponse {
  task_id: string
  status: string
}

export interface PlannedVolume {
  path: string
  device_path?: string | null
  partition_index?: number | null
  filesystem?: string
  label?: string | null
  capacity_bytes?: number
}

export interface BatchCollectCreateResponse {
  batch_id: string
  task_ids: string[]
  planned_volumes: PlannedVolume[]
  warnings: string[]
}

export type CollectCreateResponse = SingleCollectCreateResponse | BatchCollectCreateResponse

export function isBatchCollectResponse(res: CollectCreateResponse): res is BatchCollectCreateResponse {
  return 'batch_id' in res
}

export async function createCollect(body: CollectCreateBody): Promise<CollectCreateResponse> {
  const { data } = await client.post('/collect', body)
  return data
}

export type BatchStatus = 'running' | 'done' | 'partial'

export interface BatchSummary {
  done?: number
  error?: number
  cancelled?: number
  running?: number
  pending?: number
  cancelling?: number
}

export interface Batch {
  batch_id: string
  disk_id: string
  root: string
  status: BatchStatus
  planned_volumes: PlannedVolume[]
  summary: BatchSummary
  tasks: Task[]
  created_at: string
  finished_at: string | null
}

export async function getBatch(batch_id: string): Promise<Batch> {
  const { data } = await client.get(`/batches/${encodeURIComponent(batch_id)}`)
  return data
}

export interface CancelTaskResponse {
  status: string
}

export async function cancelTask(id: string): Promise<CancelTaskResponse> {
  const { data } = await client.post(`/tasks/${encodeURIComponent(id)}/cancel`)
  return data
}

export interface DeleteTaskResponse {
  deleted: boolean
  task_id: string
}

/** 删除任务登记行（只删记录，不删快照/对比等数据）。pending/running/cancelling → 409。 */
export async function deleteTask(id: string): Promise<DeleteTaskResponse> {
  const { data } = await client.delete(`/tasks/${encodeURIComponent(id)}`)
  return data
}

/** 判断错误是否为后端 404（资源不存在/已删除）。 */
export function isApiNotFound(e: unknown): boolean {
  return axios.isAxiosError(e) && e.response?.status === 404
}

export type TaskType = 'import' | 'collect' | 'hash' | 'diff' | 'build_fts' | 'build_stats'
export type TaskStatus = 'pending' | 'running' | 'cancelling' | 'cancelled' | 'done' | 'error'

export interface CollectResult {
  snapshot_id: string
  volume_id: string
  files: number
  dirs: number
  total_bytes: number
  skipped: number
  elapsed_s: number
  warnings: string[]
}

export interface Task {
  id: string
  type: TaskType
  status: TaskStatus
  progress: number | null
  message: string | null
  payload: Record<string, unknown>
  result: ({ snapshot_id?: string; skipped_import?: boolean; diff_id?: string } & Partial<CollectResult> & Record<string, unknown>) | null
  error: string | null
  related_id: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
}

export interface TasksResponse {
  items: Task[]
  next_cursor: string | null
  has_more: boolean
}

export interface ListTasksParams {
  limit?: number
  cursor?: string
  status?: string
  batch_id?: string
}

export async function listTasks(params: ListTasksParams = {}): Promise<TasksResponse> {
  const { data } = await client.get('/tasks', { params })
  return data
}

export async function getTask(id: string): Promise<Task> {
  const { data } = await client.get(`/tasks/${encodeURIComponent(id)}`)
  return data
}

export interface SettingsResponse {
  data_root: string
  version: string
}

export async function getSettings(): Promise<SettingsResponse> {
  const { data } = await client.get('/settings')
  return data
}

// ---------------------------------------------------------------- 全局搜索（顶栏 / Ctrl+K）

/** 跨快照搜索命中行（GET /api/search；只搜已封存快照，最多最近 20 个）。 */
export interface GlobalSearchItem {
  snapshot_id: string
  volume_id: string
  collected_at: string | null
  entry_id: number
  path: string
  name: string
  type: 'file' | 'dir'
  size_bytes: number | null
  mtime_ns: number | null
}

export interface GlobalSearchResponse {
  q: string
  mode: string
  items: GlobalSearchItem[]
  has_more: boolean
  next_cursor: string | null
  /** snapshots = 实际扫过的快照数；fulltext_used = 是否至少一库真正走了全文索引 */
  scanned: { snapshots: number; fulltext_used: boolean }
}

export interface GlobalSearchParams {
  /** 至少 2 个字符（后端强制，strip 后判断） */
  q: string
  mode?: 'prefix' | 'fulltext'
  limit?: number
  cursor?: string
  volume_id?: string
  snapshot_id?: string
  /** 后端只支持 file | dir */
  type?: 'file' | 'dir'
}

export async function globalSearch(params: GlobalSearchParams): Promise<GlobalSearchResponse> {
  const { data } = await client.get<GlobalSearchResponse>('/search', { params })
  return data
}

// ---------------------------------------------------------------- 卷详情

/** GET /api/volumes/{id} 返回的快照行（按采集时间升序）。 */
export interface VolumeDetailSnapshot {
  snapshot_id: string
  batch_id: string | null
  collected_at: string | null
  status: string
  hash_policy: string | null
  file_count: number | null
  dir_count: number | null
  total_bytes: number | null
  max_depth: number | null
  skipped_count: number | null
}

/** 卷详情 = volumes 行 + 所属磁盘身份 + 该卷全部快照。 */
export interface VolumeDetail {
  volume_id: string
  disk_id: string
  partition_index: number | null
  partition_uuid: string | null
  volume_serial_hex: string | null
  filesystem: string
  label: string | null
  /** 用户昵称（PATCH /volumes/{id} 设置） */
  nickname: string | null
  /** 所属磁盘昵称 */
  disk_nickname: string | null
  capacity_bytes: number | null
  notes: string | null
  physical_model: string | null
  physical_serial: string | null
  disk_capacity_bytes: number | null
  snapshots: VolumeDetailSnapshot[]
}

export async function getVolumeDetail(volume_id: string): Promise<VolumeDetail> {
  const { data } = await client.get<VolumeDetail>(`/volumes/${encodeURIComponent(volume_id)}`)
  return data
}

export default client
