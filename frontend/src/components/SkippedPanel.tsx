import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Button,
  Card,
  Empty,
  Select,
  Space,
  Statistic,
  Tag,
  Typography,
} from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import ErrorAlert from './ErrorAlert'
import CursorPager from './CursorPager'
import ResizableTable from './ResizableTable'
import { useSkipped, useSnapshot } from '../api/hooks'
import { formatNumber } from '../utils/format'
import type { SkippedItem } from '../api/client'

const { Text } = Typography

const DEFAULT_PAGE_SIZE = 50

/** scanner 产出的全部跳过原因（src/cold_manifest/scanner.py 的 warning_type 常量）。 */
const WARNING_TYPES = [
  'symlink_skipped',
  'other_filesystem',
  'excluded',
  'hidden',
  'system',
  'dangerous_name',
  'stat_error',
  'reparse_point',
  'scan_error',
]

const WARNING_COLOR: Record<string, string> = {
  scan_error: 'red',
  stat_error: 'orange',
  other_filesystem: 'gold',
  dangerous_name: 'volcano',
  excluded: 'blue',
  hidden: 'default',
  system: 'default',
  symlink_skipped: 'cyan',
  reparse_point: 'purple',
}

/**
 * 跳过项（未采集）面板：skipped 表的 keyset 分页浏览，
 * 支持 stage / warning_type 过滤；顶部按原因汇总（基于已加载条目）。
 */
export default function SkippedPanel({ snapshotId }: { snapshotId: string }) {
  const [stage, setStage] = useState<string | undefined>(undefined)
  const [warningType, setWarningType] = useState<string | undefined>(undefined)
  // cursor 栈分页：栈底 null = 第一页；上一页弹栈，下一页压入 next_cursor
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE)
  const [cursorStack, setCursorStack] = useState<(string | null)[]>([null])
  const pageIndex = cursorStack.length - 1
  const tableTopRef = useRef<HTMLDivElement | null>(null)
  const mountedRef = useRef(false)

  const { data: snapshot } = useSnapshot(snapshotId)
  const { data: page, isLoading, error } = useSkipped(snapshotId, {
    limit: pageSize,
    stage,
    warning_type: warningType,
    cursor: cursorStack[pageIndex] ?? undefined,
  })

  // 筛选变化 → 重置回第一页（渲染期同步，避免 effect 级联）
  const filterKey = `${stage ?? ''}|${warningType ?? ''}`
  const [syncedFilterKey, setSyncedFilterKey] = useState(filterKey)
  if (filterKey !== syncedFilterKey) {
    setSyncedFilterKey(filterKey)
    setCursorStack([null])
  }

  // 翻页 / 改行数后滚回表格顶部（首次挂载不滚）
  useEffect(() => {
    if (!mountedRef.current) {
      mountedRef.current = true
      return
    }
    tableTopRef.current?.scrollIntoView({ block: 'start', behavior: 'smooth' })
  }, [pageIndex, pageSize])

  const items: SkippedItem[] = useMemo(() => page?.items ?? [], [page])

  const goNext = () => {
    if (page?.has_more && page.next_cursor) setCursorStack((s) => [...s, page.next_cursor!])
  }
  const goPrev = () => setCursorStack((s) => (s.length > 1 ? s.slice(0, -1) : s))
  const changePageSize = (n: number) => {
    setPageSize(n)
    setCursorStack([null])
  }

  // 按原因汇总（基于已加载条目；全量总数见快照的 skipped_count）
  const byReason = useMemo(() => {
    const map = new Map<string, number>()
    for (const it of items) map.set(it.warning_type, (map.get(it.warning_type) ?? 0) + 1)
    return [...map.entries()].sort((a, b) => b[1] - a[1])
  }, [items])

  const total = snapshot?.skipped_count ?? 0
  const filtered = !!(stage || warningType)

  const columns = [
    { title: '路径', dataIndex: 'path', key: 'path', ellipsis: true },
    {
      title: '类型',
      dataIndex: 'warning_type',
      key: 'warning_type',
      width: 180,
      render: (v: string) => <Tag color={WARNING_COLOR[v] ?? 'default'}>{v}</Tag>,
    },
    { title: '阶段', dataIndex: 'stage', key: 'stage', width: 100 },
    {
      title: '原因',
      dataIndex: 'detail',
      key: 'detail',
      ellipsis: true,
      render: (v: string | null) => v || <Text type="secondary">-</Text>,
    },
  ]

  return (
    <div>
      {error && <ErrorAlert error={error} />}

      <Card style={{ marginBottom: 16 }}>
        <Space wrap size="large" align="start">
          <Statistic title="跳过项总数" value={formatNumber(total)} />
          <div>
            <Text type="secondary" style={{ display: 'block', marginBottom: 8 }}>按类型过滤</Text>
            <Space wrap>
              <Select
                placeholder="类型"
                allowClear
                style={{ width: 180 }}
                value={warningType}
                onChange={(v) => setWarningType(v)}
                options={WARNING_TYPES.map((t) => ({ value: t, label: t }))}
              />
              <Select
                placeholder="阶段"
                allowClear
                style={{ width: 120 }}
                value={stage}
                onChange={(v) => setStage(v)}
                options={[{ value: 'scan', label: 'scan' }]}
              />            </Space>
          </div>
        </Space>

        {byReason.length > 0 && (
          <div style={{ marginTop: 16 }}>
            <Text type="secondary" style={{ marginRight: 8 }}>
              本页 {formatNumber(items.length)}{!filtered && total > 0 ? ` / 全部 ${formatNumber(total)}` : ''} 条，按原因分布：
            </Text>
            {byReason.map(([t, n]) => (
              <Tag key={t} color={WARNING_COLOR[t] ?? 'default'} style={{ marginBottom: 4 }}>
                {t} × {formatNumber(n)}
              </Tag>
            ))}
          </div>
        )}
      </Card>

      <Card
        title={`跳过项列表${filtered ? '（已过滤）' : ''}`}
        extra={
          <Button
            icon={<ReloadOutlined />}
            size="small"
            onClick={() => {
              setStage(undefined)
              setWarningType(undefined)
            }}
            disabled={!filtered}
          >
            清除过滤
          </Button>
        }
      >
        <div ref={tableTopRef} />
        <ResizableTable
          tableId="skipped"
          rowKey={(r) => `${r.path}\u0000${r.warning_type}`}
          size="small"
          loading={isLoading}
          tableLayout="fixed"
          columns={columns}
          dataSource={items}
          pagination={false}
          scroll={{ x: 'max-content' }}
          locale={{ emptyText: <Empty description={isLoading ? '加载中…' : '没有跳过项（采集完整）'} /> }}
        />
        <CursorPager
          pageSize={pageSize}
          onPageSizeChange={changePageSize}
          canPrev={pageIndex > 0}
          onPrev={goPrev}
          canNext={page?.has_more ?? false}
          onNext={goNext}
          hint={`第 ${pageIndex + 1} 页 · 本页 ${items.length} 条`}
        />
      </Card>
    </div>
  )
}
