import { useMemo, useState } from 'react'
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
import ResizableTable from './ResizableTable'
import { useSkipped, useSnapshot } from '../api/hooks'
import { formatNumber } from '../utils/format'
import { getSkipped, type SkippedItem, type SkippedResponse } from '../api/client'

const { Text } = Typography

const PAGE_SIZE = 200

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
  // 翻页只追加 extraPages（在事件处理器里 setState）；首页由 react-query 管理
  const [extraPages, setExtraPages] = useState<SkippedResponse[]>([])
  const [loadingMore, setLoadingMore] = useState(false)

  const { data: snapshot } = useSnapshot(snapshotId)
  const { data: page, isLoading, error } = useSkipped(snapshotId, {
    limit: PAGE_SIZE,
    stage,
    warning_type: warningType,
  })

  const changeStage = (v: string | undefined) => {
    setStage(v)
    setExtraPages([])
  }
  const changeWarningType = (v: string | undefined) => {
    setWarningType(v)
    setExtraPages([])
  }

  const items = useMemo(() => {
    const seen = new Set<string>()
    const out: SkippedItem[] = []
    for (const it of [...(page?.items ?? []), ...extraPages.flatMap((p) => p.items)]) {
      const key = `${it.path}\u0000${it.warning_type}`
      if (!seen.has(key)) {
        seen.add(key)
        out.push(it)
      }
    }
    return out
  }, [page, extraPages])

  const lastPage = extraPages.length > 0 ? extraPages[extraPages.length - 1] : page
  const cursor = lastPage?.next_cursor ?? null

  const loadMore = async () => {
    if (!cursor) return
    setLoadingMore(true)
    try {
      const res = await getSkipped(snapshotId, {
        cursor,
        limit: PAGE_SIZE,
        stage,
        warning_type: warningType,
      })
      setExtraPages((prev) => [...prev, res])
    } finally {
      setLoadingMore(false)
    }
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
              />
            </Space>
          </div>
        </Space>

        {byReason.length > 0 && (
          <div style={{ marginTop: 16 }}>
            <Text type="secondary" style={{ marginRight: 8 }}>
              已加载 {formatNumber(items.length)}{!filtered && total > 0 ? ` / ${formatNumber(total)}` : ''} 条，按原因分布：
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
              changeStage(undefined)
              changeWarningType(undefined)
            }}
            disabled={!filtered}
          >
            清除过滤
          </Button>
        }
      >
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
        {cursor && (
          <div style={{ textAlign: 'center', marginTop: 16 }}>
            <Button loading={loadingMore} onClick={loadMore}>
              加载更多（已加载 {formatNumber(items.length)} 条）
            </Button>
          </div>
        )}
      </Card>
    </div>
  )
}
