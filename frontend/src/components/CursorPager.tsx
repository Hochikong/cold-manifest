import { Button, Select, Space, Typography } from 'antd'
import { LeftOutlined, RightOutlined } from '@ant-design/icons'

const { Text } = Typography

export interface CursorPagerProps {
  pageSize: number
  onPageSizeChange: (n: number) => void
  canPrev: boolean
  onPrev: () => void
  canNext: boolean
  onNext: () => void
  /** 位置提示，如「第 2 页 · 本页 50 条」；缺省不显示 */
  hint?: string
  pageSizeOptions?: number[]
  /** 深链恢复等场景下禁用翻页按钮，禁止重复触发 */
  disabled?: boolean
  /** 尺寸变化时是否重置回第一页由调用方决定，本组件只回调 */
}

/**
 * keyset cursor 分页的可复用控件：行数开关 + 上一页/下一页 + 位置提示。
 * 调用方维护 cursor 栈（上一页 = 栈内回退，下一页 = next_cursor 入栈）。
 */
export default function CursorPager({
  pageSize,
  onPageSizeChange,
  canPrev,
  onPrev,
  canNext,
  onNext,
  hint,
  pageSizeOptions = [20, 50, 100, 200],
  disabled = false,
}: CursorPagerProps) {
  return (
    <Space wrap align="center" style={{ marginTop: 16, justifyContent: 'center', width: '100%' }}>
      <Select
        size="small"
        style={{ width: 110 }}
        value={pageSize}
        onChange={onPageSizeChange}
        disabled={disabled}
        options={pageSizeOptions.map((n) => ({ value: n, label: `${n} 条/页` }))}
      />
      <Button size="small" icon={<LeftOutlined />} disabled={disabled || !canPrev} onClick={onPrev}>
        上一页
      </Button>
      <Button size="small" disabled={disabled || !canNext} onClick={onNext}>
        下一页 <RightOutlined />
      </Button>
      {hint && (
        <Text type="secondary" style={{ fontSize: 12 }}>{hint}</Text>
      )}
    </Space>
  )
}
