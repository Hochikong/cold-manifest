import { Tooltip } from 'antd'

/**
 * 表格长文本单元格：配合列 ellipsis + tableLayout="fixed" 使用。
 * 截断由表格单元格 CSS 完成；这里补充悬浮显示完整值（antd 6 不再自动加原生 title）。
 */
export default function EllipsisText({
  value,
  empty = '-',
  code = false,
}: {
  value: string | number | null | undefined
  /** 空值展示文案 */
  empty?: string
  /** 以等宽字体渲染（ID / 哈希类值） */
  code?: boolean
}) {
  const text = value == null || value === '' ? empty : String(value)
  return (
    <Tooltip title={text} placement="topLeft" mouseEnterDelay={0.3}>
      <span
        style={{
          display: 'block',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
          fontFamily: code ? 'var(--mono)' : undefined,
        }}
      >
        {text}
      </span>
    </Tooltip>
  )
}
