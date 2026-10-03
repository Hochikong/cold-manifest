/**
 * SMART / 磁盘相关枚举的中文标签映射（磁盘页各组件共用）。
 * 原则：有映射显示中文；没有映射原样显示英文；绝不显示空白。
 */

/** smart_status / health：passed / warning / failed / unavailable（无值也算无数据） */
export function smartStatusLabel(v: string | null | undefined): string {
  const s = (v ?? '').trim().toLowerCase()
  if (s === 'passed') return '正常'
  if (s === 'warning') return '警告'
  if (s === 'failed') return '异常'
  return '无数据'
}

/** smartctl -d 设备类型 */
const DEVICE_TYPE_LABELS: Record<string, string> = {
  sat: 'SATA',
  'sat,12': 'SATA（12 字节直通）',
  nvme: 'NVMe',
  scsi: 'SCSI',
  usbjmicron: 'USB 桥（SAT）',
  usbsunplus: 'USB 桥（SAT）',
  usbprolific: 'USB 桥（SAT）',
  auto: '自动',
}

export function deviceTypeLabel(v: string | null | undefined): string {
  const s = (v ?? '').trim().toLowerCase()
  if (s === '') return '未识别'
  return DEVICE_TYPE_LABELS[s] ?? (v as string)
}

/** 物理接口类型（catalog interface_type） */
const INTERFACE_LABELS: Record<string, string> = {
  sata: 'SATA',
  nvme: 'NVMe',
  usb: 'USB',
  sas: 'SAS',
  scsi: 'SCSI',
  pcie: 'PCIe',
}

export function interfaceTypeLabel(v: string | null | undefined, fallback = '-'): string {
  const s = (v ?? '').trim().toLowerCase()
  if (s === '') return fallback
  return INTERFACE_LABELS[s] ?? (v as string)
}

/** ATA 属性 when_failed 枚举（后端 when_failed_zh 缺失时的兜底） */
export function whenFailedLabel(v: string | null | undefined): string {
  const s = (v ?? '').trim()
  if (s === '') return '从未'
  if (/failing_now/i.test(s)) return '现在失败'
  if (/in_the_past/i.test(s)) return '曾失败'
  return s
}

/** ATA 尺寸规格（form_factor）：如 "3.5 inches" → "3.5 英寸" */
export function formFactorLabel(v: string | null | undefined): string {
  const s = (v ?? '').trim()
  if (s === '') return '—'
  const m = s.match(/^([\d.]+)\s*inches?$/i)
  if (m) return `${m[1]} 英寸`
  return s
}

/** zoned 计算域枚举 */
const ZONED_LABELS: Record<string, string> = {
  'not reported': '未报告',
  'host-managed': '主机管理（ZBC）',
  'host-aware': '主机感知',
  'device-managed': '设备管理',
}

export function zonedLabel(v: string | null | undefined): string {
  const s = (v ?? '').trim()
  if (s === '') return '—'
  return ZONED_LABELS[s.toLowerCase()] ?? s
}
