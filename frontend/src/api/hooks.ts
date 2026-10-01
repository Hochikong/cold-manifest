import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import {
  getHealth,
  listSnapshots,
  getSnapshot,
  getSnapshotStats,
  listEntries,
  getTree,
  getDu,
  searchEntries,
  listVolumes,
  getVolumeTrends,
  getDuplicates,
  getSkipped,
  getAttachedDisks,
  submitHashTask,
  verifySnapshotCopy,
  submitBuildIndexTask,
  patchSnapshot,
  type DuplicateMode,
  type VerifyCopyBody,
  type BuildIndexKind,
  type SnapshotPatchBody,
  listDiffs,
  createDiff,
  getDiff,
  getDiffSummary,
  listDiffEntries,
  scanImports,
  createImport,
  createCollect,
  cancelTask,
  listTasks,
  getTask,
  getBatch,
  getSettings,
  listDisks,
  getDisk,
  getDiskSmartHistory,
  collectPreflight,
  deleteSnapshot,
  type ListEntriesParams,
  type SearchParams,
  type DiffEntriesParams,
  type DiffCreateBody,
  type ImportCreateBody,
  type CollectCreateBody,
  type ListTasksParams,
  type SkippedParams,
  type HashTaskBody,
  type Task,
  type TaskStatus,
  type Batch,
} from './client'

export function useHealth() {
  return useQuery({
    queryKey: ['health'],
    queryFn: getHealth,
    staleTime: 30_000,
  })
}

export function useSnapshots(volume_id?: string) {
  return useQuery({
    queryKey: ['snapshots', volume_id],
    queryFn: () => listSnapshots(volume_id),
    staleTime: 60_000,
  })
}

export function useSnapshot(snapshot_id: string | undefined) {
  return useQuery({
    queryKey: ['snapshot', snapshot_id],
    queryFn: () => getSnapshot(snapshot_id!),
    enabled: !!snapshot_id,
    staleTime: 60_000,
  })
}

export function useSnapshotStats(snapshot_id: string | undefined) {
  return useQuery({
    queryKey: ['snapshot-stats', snapshot_id],
    queryFn: () => getSnapshotStats(snapshot_id!),
    enabled: !!snapshot_id,
    staleTime: 60_000,
  })
}

export function useEntries(snapshot_id: string | undefined, params: ListEntriesParams) {
  return useQuery({
    queryKey: ['entries', snapshot_id, params],
    queryFn: () => listEntries(snapshot_id!, params),
    enabled: !!snapshot_id,
    staleTime: 30_000,
  })
}

export function useTree(snapshot_id: string | undefined, parent_id = 0) {
  return useQuery({
    queryKey: ['tree', snapshot_id, parent_id],
    queryFn: () => getTree(snapshot_id!, parent_id),
    enabled: !!snapshot_id,
    staleTime: 30_000,
  })
}

export function useDu(snapshot_id: string | undefined, parent_id = 0, limit = 50) {
  return useQuery({
    queryKey: ['du', snapshot_id, parent_id, limit],
    queryFn: () => getDu(snapshot_id!, parent_id, limit),
    enabled: !!snapshot_id,
    staleTime: 30_000,
  })
}

export function useSearch(snapshot_id: string | undefined, params: SearchParams) {
  return useQuery({
    queryKey: ['search', snapshot_id, params],
    queryFn: () => searchEntries(snapshot_id!, params),
    enabled: !!snapshot_id && !!params.q,
    staleTime: 30_000,
  })
}

export function useVolumes() {
  return useQuery({
    queryKey: ['volumes'],
    queryFn: listVolumes,
    staleTime: 60_000,
  })
}

export function useDisks() {
  return useQuery({
    queryKey: ['disks'],
    queryFn: listDisks,
    staleTime: 60_000,
  })
}

export function useDisk(disk_id: string | undefined) {
  return useQuery({
    queryKey: ['disk', disk_id],
    queryFn: () => getDisk(disk_id!),
    enabled: !!disk_id,
    staleTime: 60_000,
  })
}

export function useDiskSmartHistory(disk_id: string | undefined) {
  return useQuery({
    queryKey: ['disk-smart', disk_id],
    queryFn: () => getDiskSmartHistory(disk_id!),
    enabled: !!disk_id,
    staleTime: 60_000,
  })
}

export function useCollectPreflight(path: string) {
  const trimmed = path.trim()
  return useQuery({
    queryKey: ['collect-preflight', trimmed],
    queryFn: () => collectPreflight({ path: trimmed }),
    enabled: trimmed.length > 0,
    staleTime: 30_000,
  })
}

export function useDeleteSnapshot() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ snapshot_id, on_disk, force }: { snapshot_id: string; on_disk: 'keep' | 'delete'; force: boolean }) =>
      deleteSnapshot(snapshot_id, { on_disk, force }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['snapshots'] })
      qc.invalidateQueries({ queryKey: ['disks'] })
      qc.invalidateQueries({ queryKey: ['volumes'] })
    },
  })
}

/** 快照列表的置顶/备注状态：列表端点不带 pinned，逐个取详情（快照总数 ≤20，可接受）。 */
export interface SnapshotPin {
  pinned: boolean
  notes: string | null
}

export function useSnapshotPins(ids: string[] | undefined) {
  const key = ids ? ids.join('|') : ''
  return useQuery({
    queryKey: ['snapshot-pins', key],
    queryFn: async () => {
      const details = await Promise.all(ids!.map((sid) => getSnapshot(sid)))
      const map: Record<string, SnapshotPin> = {}
      for (const d of details) {
        map[d.snapshot_id] = { pinned: !!d.pinned, notes: d.notes ?? null }
      }
      return map
    },
    enabled: !!ids && ids.length > 0,
    staleTime: 60_000,
  })
}

/** 更新置顶 / 备注；后端返回完整详情，直接写回详情与置顶缓存。 */
export function usePatchSnapshot() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ snapshot_id, body }: { snapshot_id: string; body: SnapshotPatchBody }) =>
      patchSnapshot(snapshot_id, body),
    onSuccess: (detail, vars) => {
      qc.setQueryData(['snapshot', vars.snapshot_id], detail)
      qc.invalidateQueries({ queryKey: ['snapshot-pins'] })
    },
  })
}

/** 同步校验盘上副本（sample / full）。 */
export function useVerifyCopy() {
  return useMutation({
    mutationFn: ({ snapshot_id, body }: { snapshot_id: string; body: VerifyCopyBody }) =>
      verifySnapshotCopy(snapshot_id, body),
  })
}

/** 提交 build-fts / build-stats 后台任务（重复提交 → 409）。 */
export function useBuildIndexTask() {
  return useMutation({
    mutationFn: ({ snapshot_id, kind }: { snapshot_id: string; kind: BuildIndexKind }) =>
      submitBuildIndexTask(snapshot_id, kind),
  })
}

export function useVolumeTrends(volume_id: string | undefined, limit?: number) {
  return useQuery({
    queryKey: ['volume-trends', volume_id, limit],
    queryFn: () => getVolumeTrends(volume_id!, limit),
    enabled: !!volume_id,
    staleTime: 60_000,
  })
}

export function useDuplicates(
  snapshot_id: string | undefined,
  params?: { mode?: DuplicateMode; min_size?: number; limit?: number; cursor?: string }
) {
  return useQuery({
    queryKey: ['duplicates', snapshot_id, params],
    queryFn: () => getDuplicates(snapshot_id!, params),
    enabled: !!snapshot_id,
    staleTime: 60_000,
  })
}

export function useDiffs() {
  return useQuery({
    queryKey: ['diffs'],
    queryFn: listDiffs,
    staleTime: 30_000,
  })
}

export function useDiff(diff_id: string | undefined) {
  return useQuery({
    queryKey: ['diff', diff_id],
    queryFn: () => getDiff(diff_id!),
    enabled: !!diff_id,
    staleTime: 30_000,
  })
}

export function useDiffSummary(diff_id: string | undefined, top = 50) {
  return useQuery({
    queryKey: ['diff-summary', diff_id, top],
    queryFn: () => getDiffSummary(diff_id!, top),
    enabled: !!diff_id,
    staleTime: 30_000,
  })
}

export function useDiffEntries(diff_id: string | undefined, params: DiffEntriesParams) {
  return useQuery({
    queryKey: ['diff-entries', diff_id, params],
    queryFn: () => listDiffEntries(diff_id!, params),
    enabled: !!diff_id,
    staleTime: 30_000,
  })
}

export function useCreateDiff() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: DiffCreateBody) => createDiff(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['diffs'] }),
  })
}

export function useImportScan(path: string | undefined, enabled = true) {
  return useQuery({
    queryKey: ['import-scan', path],
    queryFn: () => scanImports(path!),
    enabled: !!path && enabled,
    staleTime: 10_000,
  })
}

export function useCreateImport() {
  return useMutation({
    mutationFn: (body: ImportCreateBody) => createImport(body),
  })
}

export function useCreateCollect() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: CollectCreateBody) => createCollect(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['tasks'] }),
  })
}

export function useCancelTask() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => cancelTask(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['tasks'] }),
  })
}

export function useTasks(params: ListTasksParams = {}, autoRefresh = false) {
  return useQuery({
    queryKey: ['tasks', params],
    queryFn: () => listTasks(params),
    staleTime: 5_000,
    refetchInterval: (query) => {
      if (!autoRefresh) return false
      const tasks = query.state.data as { items: Task[] } | undefined
      const active = tasks?.items.some(
        (t) => t.status === 'pending' || t.status === 'running' || t.status === 'cancelling'
      )
      return active ? 4_000 : false
    },
  })
}

export function useTask(id: string | undefined, autoRefresh = false) {
  return useQuery({
    queryKey: ['task', id],
    queryFn: () => getTask(id!),
    enabled: !!id,
    staleTime: 1_000,
    refetchInterval: (query) => {
      if (!autoRefresh || !id) return false
      const task = query.state.data as Task | undefined
      return task && (task.status === 'pending' || task.status === 'running' || task.status === 'cancelling') ? 1_000 : false
    },
  })
}

export function useBatch(id: string | undefined, autoRefresh = false) {
  return useQuery({
    queryKey: ['batch', id],
    queryFn: () => getBatch(id!),
    enabled: !!id,
    staleTime: 1_000,
    refetchInterval: (query) => {
      if (!autoRefresh || !id) return false
      const batch = query.state.data as Batch | undefined
      return batch && (batch.status === 'running') ? 2_000 : false
    },
  })
}

function isTerminalStatus(status: TaskStatus): boolean {
  return status === 'done' || status === 'error' || status === 'cancelled'
}

export function useTaskEvents(taskId: string | undefined, enabled: boolean) {
  const [task, setTask] = useState<Task | null>(null)
  const [transport, setTransport] = useState<'sse' | 'poll' | null>(null)
  const retryCountRef = useRef(0)

  useEffect(() => {
    if (!enabled || !taskId) {
      setTask(null)
      setTransport(null)
      return
    }

    retryCountRef.current = 0
    let closed = false
    let es: EventSource | null = null
    let pollTimer: number | null = null

    const cleanup = () => {
      if (closed) return
      closed = true
      es?.close()
      es = null
      if (pollTimer !== null) {
        window.clearInterval(pollTimer)
        pollTimer = null
      }
    }

    const startPolling = () => {
      if (closed) return
      setTransport('poll')
      const tick = async () => {
        if (closed) return
        try {
          const t = await getTask(taskId)
          if (!closed) {
            setTask(t)
            if (isTerminalStatus(t.status)) cleanup()
          }
        } catch {
          // ignore polling errors
        }
      }
      tick()
      pollTimer = window.setInterval(tick, 1_000)
    }

    const connectSSE = () => {
      if (closed) return
      es = new EventSource(`/api/tasks/${encodeURIComponent(taskId)}/events?interval_ms=500`)

      es.onopen = () => {
        if (!closed) setTransport('sse')
      }

      es.onmessage = (event) => {
        if (closed) return
        try {
          const t: Task = JSON.parse(event.data)
          if (t === null || t === undefined) {
            cleanup()
            return
          }
          retryCountRef.current = 0
          setTask(t)
          if (isTerminalStatus(t.status)) cleanup()
        } catch {
          // ignore malformed frames
        }
      }

      es.onerror = () => {
        if (closed) return
        retryCountRef.current += 1
        if (retryCountRef.current > 2) {
          es?.close()
          es = null
          startPolling()
          return
        }
        es?.close()
        es = null
        window.setTimeout(connectSSE, 1_000)
      }
    }

    connectSSE()
    return cleanup
  }, [taskId, enabled])

  return { task, transport }
}

export function useSettings() {
  return useQuery({
    queryKey: ['settings'],
    queryFn: getSettings,
    staleTime: 60_000,
  })
}

export function useAttachedDisks(enabled = true) {
  return useQuery({
    queryKey: ['attached-disks'],
    queryFn: getAttachedDisks,
    enabled,
    staleTime: 15_000,
    retry: false,
  })
}

export function useSkipped(snapshot_id: string | undefined, params: SkippedParams) {
  return useQuery({
    queryKey: ['skipped', snapshot_id, params],
    queryFn: () => getSkipped(snapshot_id!, params),
    enabled: !!snapshot_id,
    staleTime: 30_000,
  })
}

export function useSubmitHash() {
  return useMutation({
    mutationFn: ({ snapshot_id, body }: { snapshot_id: string; body: HashTaskBody }) =>
      submitHashTask(snapshot_id, body),
  })
}

/**
 * 哈希进度概览：已算指纹/哈希的文件数。后端没有专门的计数端点，复用
 * fingerprint 查重的 hashed_files（min_size=0 时即全部已哈希文件数）。
 *
 * `enabled` 由调用方按快照的 hash_policy 传入：快照从未算过哈希（policy=none）时
 * 该端点必然 400，浏览器控制台会留下一条 "Failed to load resource" 报错，
 * 因此 policy=none 时**不发这个请求**（计数直接按 0 展示）。
 */
export function useHashSummary(snapshot_id: string | undefined, enabled = true) {
  return useQuery({
    queryKey: ['hash-summary', snapshot_id],
    queryFn: () => getDuplicates(snapshot_id!, { mode: 'fingerprint', min_size: 0, limit: 1 }),
    enabled: !!snapshot_id && enabled,
    staleTime: 15_000,
    retry: false,
  })
}
