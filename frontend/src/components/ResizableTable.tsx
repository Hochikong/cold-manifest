/**
 * 可拖拽列宽的 AntD Table 封装（基于官方 react-resizable recipe）。
 *
 * - 只有显式声明了数字 width 的列可拖拽；最小宽度 64。
 * - 列宽按表持久化到 localStorage（key: `cldm_colw:<tableId>`）。
 * - 在表头任意单元格上右键 → 重置该表全部列宽。
 * - 配合 tableLayout="fixed" + 列 ellipsis + scroll={{ x: 'max-content' }} 使用：
 *   拖宽后可横向滚动，不会挤压相邻列。
 */
import { useCallback, useMemo, useState } from 'react'
import { App, Table } from 'antd'
import type { TableProps } from 'antd'
import { Resizable } from 'react-resizable'
import 'react-resizable/css/styles.css'

const MIN_WIDTH = 64

function colKeyOf(col: TableProps<any>['columns'] extends (infer C)[] | undefined ? C : never): string | undefined {
  if (col.key !== undefined) return String(col.key)
  const di = (col as { dataIndex?: unknown }).dataIndex
  if (typeof di === 'string') return di
  if (Array.isArray(di)) return di.join('.')
  return undefined
}

/** 表头单元格：包一层 Resizable，拖拽手柄为右侧 10px 热区。 */
function ResizableTitle(props: Record<string, unknown>) {
  const { width, onResize, onResetColumns, ...rest } = props
  const w = width as number | undefined
  const resize = onResize as ((e: unknown, data: { size: { width: number } }) => void) | undefined
  const reset = onResetColumns as (() => void) | undefined
  if (!w || !resize) return <th {...rest} />
  return (
    <Resizable
      width={w}
      height={0}
      handle={<span className="cldm-col-resize-handle" onClick={(e) => e.stopPropagation()} />}
      onResize={(_e, { size }) => resize(_e, { size })}
      draggableOpts={{ enableUserSelectHack: false }}
    >
      <th
        {...rest}
        onContextMenu={(e) => {
          // 表头右键 → 重置列宽（该表的 localStorage 记录一并清除）
          if (reset) {
            e.preventDefault()
            reset()
          }
        }}
      />
    </Resizable>
  )
}

type ResizableTableProps<T> = TableProps<T> & { tableId: string }

export default function ResizableTable<T extends object = any>({
  tableId,
  columns,
  components,
  ...rest
}: ResizableTableProps<T>) {
  const { message } = App.useApp()
  const storageKey = `cldm_colw:${tableId}`
  const [widths, setWidths] = useState<Record<string, number>>(() => {
    try {
      return JSON.parse(localStorage.getItem(storageKey) || '{}') as Record<string, number>
    } catch {
      return {}
    }
  })

  const setWidth = useCallback(
    (key: string, w: number) => {
      setWidths((prev) => {
        const next = { ...prev, [key]: Math.max(MIN_WIDTH, Math.round(w)) }
        try {
          localStorage.setItem(storageKey, JSON.stringify(next))
        } catch {
          // 存储不可用时列宽仅在当前页面生效
        }
        return next
      })
    },
    [storageKey]
  )

  const resetColumns = useCallback(() => {
    setWidths({})
    try {
      localStorage.removeItem(storageKey)
    } catch {
      // ignore
    }
    message.success('已重置列宽')
  }, [storageKey, message])

  const mergedColumns = useMemo(() => {
    return columns?.map((col) => {
      const key = colKeyOf(col)
      const base = typeof col.width === 'number' ? col.width : undefined
      // 没声明宽度的列（如长路径列）保持自动伸展，不可拖拽
      if (!key || base === undefined) return col
      const w = widths[key] ?? base
      const prevOnHeaderCell = col.onHeaderCell
      return {
        ...col,
        width: w,
        onHeaderCell: (data: any) => ({
          style: { position: 'relative' },
          width: Math.max(w, MIN_WIDTH),
          onResize: (_e: unknown, { size }: { size: { width: number } }) => setWidth(key, size.width),
          onResetColumns: resetColumns,
          ...(prevOnHeaderCell?.(data) || {}),
        }),
      } as typeof col
    })
  }, [columns, widths, setWidth, resetColumns])

  return (
    <Table<T>
      {...rest}
      columns={mergedColumns}
      components={{
        ...(components || {}),
        header: { cell: ResizableTitle, ...(components?.header || {}) },
      }}
    />
  )
}
