import dayjs from 'dayjs'
import relativeTime from 'dayjs/plugin/relativeTime'

dayjs.extend(relativeTime)

export function formatFileSize(bytes: number | null | undefined): string {
  if (bytes == null || Number.isNaN(bytes)) return '-'
  if (bytes < 0) return '-'
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
  let value = bytes
  let i = 0
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024
    i++
  }
  if (i === 0) return `${value} B`
  return `${value.toFixed(1)} ${units[i]}`
}

export function formatDateTime(value: string | number | Date | null | undefined): string {
  if (value == null || value === '') return '-'
  const d = dayjs(value)
  return d.isValid() ? d.format('YYYY-MM-DD HH:mm') : '-'
}

export function formatNumber(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return '-'
  return n.toLocaleString('zh-CN')
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms == null || Number.isNaN(ms)) return '-'
  const seconds = Math.floor(ms / 1000)
  if (seconds < 60) return `${seconds} 秒`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} 分 ${seconds % 60} 秒`
  const hours = Math.floor(minutes / 60)
  return `${hours} 小时 ${minutes % 60} 分`
}

export function formatPercent(n: number | null | undefined, digits = 1): string {
  if (n == null || Number.isNaN(n)) return '-'
  return `${(n * 100).toFixed(digits)}%`
}

export function nsToDate(ns: string | number | null | undefined): string {
  if (ns == null || ns === '') return '-'
  const ms = typeof ns === 'string' ? Number(ns) / 1_000_000 : ns / 1_000_000
  return formatDateTime(ms)
}

export function formatRelativeTime(value: string | number | Date | null | undefined): string {
  if (value == null || value === '') return '-'
  const d = dayjs(value)
  if (!d.isValid()) return '-'
  return d.fromNow()
}

/**
 * 中间截断：保留首尾（序列号常靠头尾辨识），如 WD80EAZZ…3KL-1RTS。
 * 超过 max 才截，配合悬浮显示完整值使用。
 */
export function truncateMiddle(value: string | null | undefined, max = 24): string {
  if (value == null) return '-'
  const s = String(value)
  if (s.length <= max) return s
  const head = Math.ceil((max - 1) / 2)
  const tail = Math.floor((max - 1) / 2)
  return `${s.slice(0, head)}…${s.slice(s.length - tail)}`
}
