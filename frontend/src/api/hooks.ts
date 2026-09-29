import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
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
  listDiffs,
  createDiff,
  getDiff,
  getDiffSummary,
  listDiffEntries,
  scanImports,
  createImport,
  listTasks,
  getTask,
  getSettings,
  type ListEntriesParams,
  type SearchParams,
  type DiffEntriesParams,
  type DiffCreateBody,
  type ImportCreateBody,
  type ListTasksParams,
  type Task,
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

export function useTasks(params: ListTasksParams = {}, autoRefresh = false) {
  return useQuery({
    queryKey: ['tasks', params],
    queryFn: () => listTasks(params),
    staleTime: 5_000,
    refetchInterval: (query) => {
      if (!autoRefresh) return false
      const tasks = query.state.data as { items: Task[] } | undefined
      const active = tasks?.items.some((t) => t.status === 'pending' || t.status === 'running')
      return active ? 2_000 : false
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
      return task && (task.status === 'pending' || task.status === 'running') ? 1_000 : false
    },
  })
}

export function useSettings() {
  return useQuery({
    queryKey: ['settings'],
    queryFn: getSettings,
    staleTime: 60_000,
  })
}
