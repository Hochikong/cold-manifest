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
  disk_id: string
  filesystem: string
  label: string | null
  volume_capacity_bytes: number
  physical_model: string
  physical_serial: string | null
  disk_capacity_bytes: number
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

export async function getSnapshot(snapshot_id: string): Promise<Snapshot & { volume: Record<string, unknown>; on_disk_copy: Record<string, unknown> | null; meta: Record<string, string> }> {
  const { data } = await client.get(`/snapshots/${encodeURIComponent(snapshot_id)}`)
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

export interface LatestSmart {
  health: string
  temperature_c: number | null
  power_on_hours: number | null
  reallocated_ct: number | null
  pending_ct: number | null
  collected_at: string
}

export interface Disk {
  disk_id: string
  physical_model: string | null
  physical_serial: string | null
  bridge_model: string | null
  capacity_bytes: number
  interface_type: string | null
  first_seen: string
  last_seen: string
  volume_count: number
  snapshot_count: number
  latest_smart: LatestSmart | null
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
}

export async function getDisk(disk_id: string): Promise<DiskDetail> {
  const { data } = await client.get(`/disks/${encodeURIComponent(disk_id)}`)
  return data
}

export interface SmartItem {
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
}

export interface HashTaskResponse {
  task_id: string
  status: string
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
  count: number
}

export async function listDiffs(): Promise<DiffsResponse> {
  const { data } = await client.get('/diffs')
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

export type TaskType = 'import' | 'collect' | 'hash' | 'diff'
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

export default client
