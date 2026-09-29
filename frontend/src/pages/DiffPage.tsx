import { useEffect, useMemo, useState } from 'react'
import { useSearchParams, useNavigate } from 'react-router-dom'
import {
  Card,
  Row,
  Col,
  Statistic,
  Select,
  Button,
  Space,
  Table,
  Tag,
  Typography,
  Empty,
  Spin,
  Skeleton,
  Alert,
  Drawer,
  Badge,
} from 'antd'
import {
  SwapOutlined,
  PlayCircleOutlined,
  DownloadOutlined,
  ArrowLeftOutlined,
} from '@ant-design/icons'
import ReactECharts from 'echarts-for-react'
import { useSnapshots, useDiffs, useDiff, useDiffSummary, useDiffEntries, useCreateDiff } from '../api/hooks'
import ErrorAlert from '../components/ErrorAlert'
import { formatFileSize, formatDateTime, nsToDate } from '../utils/format'
import { exportDiffUrl, diffReportUrl, type DiffEntry } from '../api/client'

const { Title, Text } = Typography

const CATEGORY_LABELS: Record<string, { label: string; color: string }> = {
  added: { label: '新增', color: 'green' },
  removed: { label: '删除', color: 'red' },
  size_changed: { label: '大小变更', color: 'orange' },
  mtime_changed: { label: '时间变更', color: 'gold' },
  content_changed: { label: '内容变更', color: 'volcano' },
  type_changed: { label: '类型变更', color: 'purple' },
  moved_or_renamed: { label: '改名/移动', color: 'blue' },
  identical: { label: '一致', color: 'default' },
}

const ALL_CATEGORIES = ['added', 'removed', 'size_changed', 'mtime_changed', 'content_changed', 'type_changed', 'moved_or_renamed']

export default function DiffPage() {
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const diffId = searchParams.get('id') || undefined

  if (!diffId) {
    return <DiffSelector />
  }

  return (
    <div>
      <Space align="center" style={{ marginBottom: 16 }}>
        <Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/diff')}>
          返回选择
        </Button>
        <Title level={4} style={{ margin: 0 }}>对比：{diffId}</Title>
      </Space>
      <DiffDetail diffId={diffId} />
    </div>
  )
}

function DiffSelector() {
  const { data: snapshots, isLoading: snapLoading, error: snapError } = useSnapshots()
  const { data: diffs, isLoading: diffLoading, error: diffError } = useDiffs()
  const create = useCreateDiff()
  const navigate = useNavigate()

  const [a, setA] = useState<string | undefined>(undefined)
  const [b, setB] = useState<string | undefined>(undefined)

  const snapshotOptions = useMemo(
    () =>
      (snapshots?.items || []).map((s) => ({
        value: s.snapshot_id,
        label: `${s.volume_id} / ${s.snapshot_id.split('/')[1]}`,
      })),
    [snapshots]
  )

  const start = () => {
    if (!a || !b) return
    create.mutate(
      { a, b, options: { hash: 'none' } },
      {
        onSuccess: (res) => {
          navigate(`/diff?id=${res.diff_id}`)
        },
      }
    )
  }

  const error = snapError || diffError

  return (
    <div>
      <Title level={4} style={{ marginTop: 0 }}>对比</Title>
      {error && <ErrorAlert error={error} />}

      <Card title="发起新对比" style={{ marginBottom: 16 }}>
        <Space wrap align="center">
          <Select
            placeholder="选择 A 快照"
            style={{ width: 320 }}
            options={snapshotOptions}
            value={a}
            onChange={setA}
            loading={snapLoading}
          />
          <Button icon={<SwapOutlined />} onClick={() => { const tmp = a; setA(b); setB(tmp) }} />
          <Select
            placeholder="选择 B 快照"
            style={{ width: 320 }}
            options={snapshotOptions}
            value={b}
            onChange={setB}
            loading={snapLoading}
          />
          <Button
            type="primary"
            icon={<PlayCircleOutlined />}
            loading={create.isPending}
            disabled={!a || !b}
            onClick={start}
          >
            开始对比
          </Button>
        </Space>
        {create.isError && <Alert style={{ marginTop: 16 }} type="error" title={String(create.error)} />}
      </Card>

      <Card title="历史对比">
        {diffLoading ? (
          <Skeleton active />
        ) : diffs?.items.length ? (
          <Table
            rowKey="diff_id"
            size="small"
            pagination={{ pageSize: 10 }}
            dataSource={diffs.items}
            columns={[
              { title: 'ID', dataIndex: 'diff_id', render: (v: string) => <Button type="link" style={{ padding: 0 }} onClick={() => navigate(`/diff?id=${v}`)}>{v}</Button> },
              { title: 'A', dataIndex: 'a', ellipsis: true },
              { title: 'B', dataIndex: 'b', ellipsis: true },
              { title: '创建时间', dataIndex: 'created_at', render: (v: string) => formatDateTime(v) },
              { title: '耗时', dataIndex: 'duration_ms', render: (v: number) => `${(v / 1000).toFixed(1)} 秒` },
              {
                title: '差异',
                render: (_: unknown, record: { summary: Record<string, number | null> }) => (
                  <Space size={4}>
                    <Tag color="green">+{record.summary?.added ?? 0}</Tag>
                    <Tag color="red">-{record.summary?.removed ?? 0}</Tag>
                    <Tag color="orange">~{record.summary?.size_changed ?? 0}</Tag>
                  </Space>
                ),
              },
            ]}
          />
        ) : (
          <Empty description="暂无对比记录" />
        )}
      </Card>
    </div>
  )
}

function DiffDetail({ diffId }: { diffId: string }) {
  const { data: diff, isLoading: diffLoading, error: diffError } = useDiff(diffId)
  const { data: summary, isLoading: summaryLoading, error: summaryError } = useDiffSummary(diffId, 50)
  const [category, setCategory] = useState<string | undefined>(undefined)
  const [drawerEntry, setDrawerEntry] = useState<DiffEntry | null>(null)

  const counts = diff?.summary || {}
  const error = diffError || summaryError

  const pieOption = useMemo(() => {
    const data = ALL_CATEGORIES
      .filter((k) => counts[k])
      .map((k) => ({ name: CATEGORY_LABELS[k]?.label || k, value: counts[k] }))
    if (!data.length) return null
    return {
      tooltip: { trigger: 'item' },
      legend: { bottom: 0 },
      series: [
        {
          type: 'pie',
          radius: ['40%', '70%'],
          data,
          label: { show: true, formatter: '{b}: {c}' },
        },
      ],
    }
  }, [counts])

  return (
    <div>
      {error && <ErrorAlert error={error} />}
      {(diffLoading || summaryLoading) && <Spin style={{ display: 'block', margin: '32px auto' }} />}

      {diff && (
        <Card style={{ marginBottom: 16 }}>
          <Space orientation="vertical" style={{ width: '100%' }}>
            <Text>A：<Text code>{diff.a}</Text></Text>
            <Text>B：<Text code>{diff.b}</Text></Text>
            <Text>创建时间：{formatDateTime(diff.created_at)}</Text>
            <Text>耗时：{(diff.duration_ms / 1000).toFixed(1)} 秒</Text>
            <Text>选项：{JSON.stringify(diff.options)}</Text>
          </Space>
        </Card>
      )}

      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        {ALL_CATEGORIES.map((k) => (
          <Col xs={12} sm={8} md={6} lg={3} key={k}>
            <Card
              style={{
                cursor: 'pointer',
                borderColor: category === k ? CATEGORY_LABELS[k].color : undefined,
              }}
              onClick={() => setCategory(category === k ? undefined : k)}
            >
              <Statistic
                title={CATEGORY_LABELS[k].label}
                value={counts[k] ?? 0}
                styles={{ content: { color: category === k ? CATEGORY_LABELS[k].color : undefined } }}
              />
              <Badge color={CATEGORY_LABELS[k].color} />
            </Card>
          </Col>
        ))}
      </Row>

      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col xs={24} md={12}>
          <Card title="差异构成">
            {summaryLoading ? (
              <Skeleton active paragraph={{ rows: 5 }} />
            ) : pieOption ? (
              <ReactECharts option={pieOption} style={{ height: 320 }} />
            ) : (
              <Empty description="无差异" />
            )}
          </Card>
        </Col>
        <Col xs={24} md={12}>
          <Card title="目录维度 Top 变更">
            {summaryLoading ? (
              <Skeleton active />
            ) : summary?.by_parent_dir.length ? (
              <Table
                rowKey="parent_dir"
                size="small"
                pagination={false}
                scroll={{ y: 320 }}
                dataSource={summary.by_parent_dir}
                columns={[
                  { title: '目录', dataIndex: 'parent_dir', ellipsis: true },
                  { title: '变更数', dataIndex: 'count', width: 90 },
                  {
                    title: '净大小变化',
                    dataIndex: 'size_delta',
                    width: 130,
                    render: (v: number) => (
                      <Text style={{ color: v > 0 ? '#52c41a' : v < 0 ? '#ff4d4f' : undefined }}>
                        {v === 0 ? '—' : `${v > 0 ? '+' : ''}${formatFileSize(v)}`}
                      </Text>
                    ),
                  },
                ]}
              />
            ) : (
              <Empty />
            )}
          </Card>
        </Col>
      </Row>

      <Card
        title={
          <Space>
            <span>文件明细</span>
            {category && (
              <Tag
                color={CATEGORY_LABELS[category]?.color}
                closable
                onClose={() => setCategory(undefined)}
              >
                {CATEGORY_LABELS[category]?.label}
              </Tag>
            )}
          </Space>
        }
        extra={
          <Space>
            <Button icon={<DownloadOutlined />} href={diffReportUrl(diffId)} disabled={diff?.status !== 'done'}>
              下载 HTML 报告
            </Button>
            <Button icon={<DownloadOutlined />} href={exportDiffUrl(diffId)}>
              导出 CSV
            </Button>
          </Space>
        }
      >
        <DiffEntriesTable diffId={diffId} category={category} onRowClick={setDrawerEntry} />
      </Card>

      <Drawer
        title={drawerEntry?.path}
        open={!!drawerEntry}
        onClose={() => setDrawerEntry(null)}
        size={560}
      >
        {drawerEntry && <DiffEntryDetail entry={drawerEntry} />}
      </Drawer>
    </div>
  )
}

function DiffEntriesTable({
  diffId,
  category,
  onRowClick,
}: {
  diffId: string
  category: string | undefined
  onRowClick: (e: DiffEntry) => void
}) {
  const [allItems, setAllItems] = useState<DiffEntry[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [loadingMore, setLoadingMore] = useState(false)

  const { data, isLoading, error } = useDiffEntries(diffId, {
    category,
    limit: 200,
    sort: 'path',
    order: 'asc',
  })

  useEffect(() => {
    if (data) {
      setAllItems(data.items)
      setCursor(data.next_cursor)
    }
  }, [data])

  const loadMore = async () => {
    if (!cursor) return
    setLoadingMore(true)
    const res = await fetch(
      `/api/diffs/${diffId}/entries?limit=200&sort=path&order=asc${category ? `&category=${category}` : ''}&cursor=${encodeURIComponent(cursor)}`
    )
    const json = await res.json()
    setAllItems((prev) => [...prev, ...json.items])
    setCursor(json.next_cursor)
    setLoadingMore(false)
  }

  const columns = [
    {
      title: '路径',
      dataIndex: 'path',
      ellipsis: true,
      render: (v: string, record: DiffEntry) => (
        <Button type="link" style={{ padding: 0 }} onClick={() => onRowClick(record)}>
          {v}
        </Button>
      ),
    },
    {
      title: '类型',
      dataIndex: 'category',
      width: 100,
      render: (v: string) => (
        <Tag color={CATEGORY_LABELS[v]?.color || 'default'}>{CATEGORY_LABELS[v]?.label || v}</Tag>
      ),
    },
    {
      title: 'A 大小',
      dataIndex: 'a_size',
      width: 120,
      render: (v: number | null, record: DiffEntry) => (
        <Text style={{ color: record.a_size !== record.b_size ? '#ff4d4f' : undefined }}>
          {formatFileSize(v)}
        </Text>
      ),
    },
    {
      title: 'B 大小',
      dataIndex: 'b_size',
      width: 120,
      render: (v: number | null, record: DiffEntry) => (
        <Text style={{ color: record.a_size !== record.b_size ? '#ff4d4f' : undefined }}>
          {formatFileSize(v)}
        </Text>
      ),
    },
    {
      title: 'A 修改时间',
      dataIndex: 'a_mtime_ns',
      width: 170,
      render: (v: string | null) => nsToDate(v),
    },
    {
      title: 'B 修改时间',
      dataIndex: 'b_mtime_ns',
      width: 170,
      render: (v: string | null) => nsToDate(v),
    },
  ]

  return (
    <div>
      {error && <ErrorAlert error={error} />}
      <Table
        rowKey="id"
        size="small"
        loading={isLoading}
        columns={columns}
        dataSource={allItems}
        pagination={false}
        scroll={{ x: 'max-content' }}
        locale={{ emptyText: <Empty description="无差异文件" /> }}
      />
      {cursor && (
        <div style={{ textAlign: 'center', marginTop: 16 }}>
          <Button loading={loadingMore} onClick={loadMore}>加载更多</Button>
        </div>
      )}
    </div>
  )
}

function DiffEntryDetail({ entry }: { entry: DiffEntry }) {
  const labelStyle: React.CSSProperties = { width: 100, display: 'inline-block', color: '#888' }
  return (
    <Space orientation="vertical" style={{ width: '100%' }} size="large">
      <div>
        <Text strong>A 快照</Text>
        <div><Text style={labelStyle}>路径</Text>{entry.path || '—'}</div>
        <div><Text style={labelStyle}>类型</Text>{entry.a_type || '—'}</div>
        <div><Text style={labelStyle}>大小</Text>{formatFileSize(entry.a_size)}</div>
        <div><Text style={labelStyle}>修改时间</Text>{nsToDate(entry.a_mtime_ns)}</div>
      </div>
      <div>
        <Text strong>B 快照</Text>
        <div><Text style={labelStyle}>路径</Text>{entry.path || '—'}</div>
        <div><Text style={labelStyle}>类型</Text>{entry.b_type || '—'}</div>
        <div><Text style={labelStyle}>大小</Text>{formatFileSize(entry.b_size)}</div>
        <div><Text style={labelStyle}>修改时间</Text>{nsToDate(entry.b_mtime_ns)}</div>
      </div>
    </Space>
  )
}
