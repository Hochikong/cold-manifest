import type { Task, TaskStatus, TaskType } from '../api/client'
import { formatDuration, formatFileSize, formatNumber } from './format'

export const TASK_TYPE_LABELS: Record<TaskType | string, string> = {
  collect: '采集',
  import: '导入',
  hash: '哈希',
  diff: '对比',
}

export const TASK_STATUS_LABELS: Record<TaskStatus, string> = {
  pending: '待处理',
  running: '运行中',
  cancelling: '取消中',
  cancelled: '已取消',
  done: '完成',
  error: '失败',
}

export function taskTypeLabel(type: TaskType | string): string {
  return TASK_TYPE_LABELS[type] ?? type
}

export function statusLabel(status: TaskStatus): string {
  return TASK_STATUS_LABELS[status] ?? status
}

export function statusColor(status: TaskStatus): string {
  switch (status) {
    case 'done':
      return 'success'
    case 'error':
      return 'error'
    case 'cancelled':
      return 'warning'
    default:
      return 'default'
  }
}

export function isTerminalStatus(status: TaskStatus): boolean {
  return status === 'done' || status === 'error' || status === 'cancelled'
}

function snapshotName(id: string | unknown): string {
  if (typeof id !== 'string') return ''
  const parts = id.split('/')
  return parts.length > 1 ? `${parts[0]}/${parts[1]}` : id
}

export function getTaskSummary(task: Task): string {
  const result = task.result ?? {}
  const type = task.type
  const status = task.status

  if (status === 'error') {
    return task.error ? `失败：${task.error}` : '任务失败'
  }
  if (status === 'cancelled') {
    return '任务已取消'
  }

  if (status !== 'done') {
    return '任务运行中'
  }

  if (type === 'collect') {
    const files = typeof result.files === 'number' ? result.files : undefined
    const dirs = typeof result.dirs === 'number' ? result.dirs : undefined
    const bytes = typeof result.total_bytes === 'number' ? result.total_bytes : undefined
    const elapsed = typeof result.elapsed_s === 'number' ? result.elapsed_s * 1000 : undefined
    const parts: string[] = []
    if (files !== undefined) parts.push(`文件 ${formatNumber(files)}`)
    if (dirs !== undefined) parts.push(`目录 ${formatNumber(dirs)}`)
    if (bytes !== undefined) parts.push(formatFileSize(bytes))
    if (elapsed !== undefined) parts.push(`耗时 ${formatDuration(elapsed)}`)
    return parts.length ? parts.join(' / ') : '采集完成'
  }

  if (type === 'import') {
    if (result.skipped_import) {
      return '导入已跳过（快照已存在）'
    }
    const sid = result.snapshot_id
    if (typeof sid === 'string') {
      return `已导入快照 ${snapshotName(sid)}`
    }
    return '导入完成'
  }

  if (type === 'hash') {
    const sid = result.snapshot_id
    const hashed = typeof result.hashed === 'number' ? result.hashed : undefined
    const errors = typeof result.errors === 'number' ? result.errors : undefined
    const elapsed = typeof result.elapsed_s === 'number' ? result.elapsed_s * 1000 : undefined
    const parts: string[] = []
    if (typeof sid === 'string') parts.push(`快照 ${snapshotName(sid)}`)
    if (hashed !== undefined) parts.push(`已哈希 ${formatNumber(hashed)}`)
    if (errors !== undefined && errors > 0) parts.push(`错误 ${formatNumber(errors)}`)
    if (elapsed !== undefined) parts.push(`耗时 ${formatDuration(elapsed)}`)
    return parts.length ? parts.join(' / ') : '哈希完成'
  }

  if (type === 'diff') {
    const diffId = result.diff_id
    const total = typeof result.total === 'number' ? result.total : undefined
    const elapsed = typeof result.elapsed_s === 'number' ? result.elapsed_s * 1000 : undefined
    const parts: string[] = []
    if (typeof diffId === 'string') parts.push(`对比 ${diffId}`)
    if (total !== undefined) parts.push(`差异 ${formatNumber(total)}`)
    if (elapsed !== undefined) parts.push(`耗时 ${formatDuration(elapsed)}`)
    return parts.length ? parts.join(' / ') : '对比完成'
  }

  return '任务完成'
}
