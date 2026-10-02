import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams, useNavigate } from 'react-router-dom'
import {
  App,
  Card,
  Row,
  Col,
  Statistic,
  Select,
  Button,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
  Empty,
  Spin,
  Skeleton,
  Alert,
  Drawer,
  Badge,
  Input,
} from 'antd'
import type { TableProps } from 'antd'
import {
  SwapOutlined,
  PlayCircleOutlined,
  DownloadOutlined,
  ArrowLeftOutlined,
  FilterOutlined,
  DeleteOutlined,
} from '@ant-design/icons'
import ReactECharts from 'echarts-for-react'
import { useSnapshots, useDiffs, useDiff, useDiffSummary, useDiffEntries, useCreateDiff, useDeleteDiff } from '../api/hooks'
import ErrorAlert from '../components/ErrorAlert'
import EllipsisText from '../components/EllipsisText'
import ResizableTable from '../components/ResizableTable'
import CursorPager from '../components/CursorPager'
import { useRowContextMenu, type RowContextTarget } from '../hooks/useRowContextMenu'
import { useShowApiError } from '../utils/apiError'
import { formatFileSize, formatDateTime, nsToDate } from '../utils/format'
import { dirNameOf } from '../utils/path'
import { apiErrorDetail, exportDiffUrl, diffReportUrl, type DiffEntry, type DiffRun } from '../api/client'

const { Title, Text } = Typography

const EVIDENCE_LABELS: Record<string, string> = {
  hash: '哈希级',
  'size+mtime': '大小+时间',
  unavailable: '不可用',
  size: '文件大小',
  mtime: '修改时间',
  type: '类型',
}

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

function EvidenceTag({ level, evidence }: { level?: string; evidence?: Record<string, string> }) {
  if (!level) return <Tag>未知</Tag>
  const isHash = level === 'hash'
  const tooltip = (
    <Space orientation="vertical" size={0}>
      <Text>{isHash ? '哈希级＝内容变更由完整哈希判定，可信。' : '大小+时间＝未启用哈希时的退化口径，同名同大小同时间的改写可能漏判。'}</Text>
      {evidence && (
        <div>
          {Object.entries(evidence).map(([key, value]) => (
            <div key={key}>
              <Text strong>{CATEGORY_LABELS[key]?.label || key}：</Text>
              <Text>{EVIDENCE_LABELS[value] || value}</Text>
            </div>
          ))}
        </div>
      )}
    </Space>
  )
  return (
    <Tooltip title={tooltip}>
      <Tag color={isHash ? 'success' : 'default'}>{isHash ? '哈希级' : '大小+时间'}</Tag>
    </Tooltip>
  )
}

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
  const { modal, message } = App.useApp()
  const { data: snapshots, isLoading: snapLoading, error: snapError } = useSnapshots()
  const create = useCreateDiff()
  const del = useDeleteDiff()
  const showApiError = useShowApiError()
  const navigate = useNavigate()

  // 历史对比列表：cursor 栈分页（上一页 = 栈内回退，下一页 = next_cursor 入栈）
  const [historySize, setHistorySize] = useState(20)
  const [cursorStack, setCursorStack] = useState<(string | null)[]>([null])
  const historyIndex = cursorStack.length - 1
  const { data: historyPage, isLoading: diffLoading, error: diffError } = useDiffs({
    limit: historySize,
    cursor: cursorStack[historyIndex] ?? undefined,
  })
  const historyItems = historyPage?.items ?? []

  const changeHistorySize = (n: number) => {
    setHistorySize(n)
    setCursorStack([null])
  }

  const confirmDeleteDiff = (record: DiffRun) => {
    modal.confirm({
      title: '删除这份对比结果？',
      content: `将删除 ${record.diff_id} 的对比结果（不影响两侧快照），删除后不可恢复。`,
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: () =>
        new Promise<void>((resolve, reject) => {
          del.mutate(record.diff_id, {
            onSuccess: () => {
              message.success('对比结果已删除')
              resolve()
            },
            onError: (e) => {
              showApiError(e, '删除失败')
              reject(e)
            },
          })
        }),
    })
  }

  const historyColumns: TableProps<DiffRun>['columns'] = [
    {
      title: 'ID',
      dataIndex: 'diff_id',
      width: 280,
      ellipsis: true,
      render: (v: string) => (
        <Tooltip title={v}>
          <Button type="link" style={{ padding: 0 }} onClick={() => navigate(`/diff?id=${v}`)}>{v}</Button>
        </Tooltip>
      ),
    },
    {
      title: 'A',
      dataIndex: 'a',
      width: 260,
      ellipsis: true,
      render: (_v: string, record) => <EllipsisText value={record.labels?.a || record.a} code />,
    },
    {
      title: 'B',
      dataIndex: 'b',
      width: 260,
      ellipsis: true,
      render: (_v: string, record) => <EllipsisText value={record.labels?.b || record.b} code />,
    },
    { title: '创建时间', dataIndex: 'created_at', width: 150, render: (v: string) => formatDateTime(v) },
    { title: '耗时', dataIndex: 'duration_ms', width: 90, render: (v: number) => `${(v / 1000).toFixed(1)} 秒` },
    {
      title: '差异',
      width: 190,
      render: (_, record) => (
        <Space size={4}>
          <Tag color="green">+{record.summary?.added ?? 0}</Tag>
          <Tag color="red">-{record.summary?.removed ?? 0}</Tag>
          <Tag color="orange">~{record.summary?.size_changed ?? 0}</Tag>
        </Space>
      ),
    },
    {
      title: '操作',
      width: 70,
      render: (_, record) => (
        <Tooltip title="删除这份对比结果">
          <Button
            type="text"
            size="small"
            danger
            aria-label={`删除对比 ${record.diff_id}`}
            icon={<DeleteOutlined />}
            onClick={() => confirmDeleteDiff(record)}
          />
        </Tooltip>
      ),
    },
  ]

  const [a, setA] = useState<string | undefined>(undefined)
  const [b, setB] = useState<string | undefined>(undefined)
  const [hash, setHash] = useState<'none' | 'sha256'>('none')
  const [caseInsensitive, setCaseInsensitive] = useState(false)
  const [ignoreMtime, setIgnoreMtime] = useState(false)
  const [ignoreSize, setIgnoreSize] = useState(false)
  const [showIdentical, setShowIdentical] = useState(false)

  const snapshotOptions = useMemo(
    () =>
      (snapshots?.items || []).map((s) => {
        const ts = s.snapshot_id.split('/')[1] ?? s.snapshot_id
        // 有昵称用 label（如「视频分区（移动硬盘A） · DEMO01_P0」），没有回落 卷ID / 时间戳目录
        const name = s.label || `${s.volume_id} / ${ts}`
        return {
          value: s.snapshot_id,
          label: `${name} / ${formatDateTime(s.collected_at)}`,
        }
      }),
    [snapshots]
  )

  const start = () => {
    if (!a || !b) return
    create.mutate(
      {
        a,
        b,
        options: {
          hash,
          case_insensitive: caseInsensitive,
          ignore_mtime: ignoreMtime,
          ignore_size: ignoreSize,
          show_identical: showIdentical,
        },
      },
      {
        onSuccess: (res) => {
          navigate(`/diff?id=${res.diff_id}`)
        },
        // 后端 400（如「无法 ATTACH 快照库：…」）必须把中文原因展示出来，
        // 不能只留一条 AxiosError 给控制台
        onError: (e) => {
          showApiError(e, '对比发起失败')
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
          <Select
            value={hash}
            onChange={setHash}
            style={{ width: 150 }}
            options={[
              { value: 'none', label: '不比对内容' },
              { value: 'sha256', label: 'sha256 内容比对' },
            ]}
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
        <Space wrap style={{ marginTop: 12 }}>
          <Tooltip title="路径比较时忽略大小写差异">
            <Space>
              <Switch
                size="small"
                checked={caseInsensitive}
                onChange={setCaseInsensitive}
                id="diff-case-insensitive"
              />
              <label htmlFor="diff-case-insensitive" style={{ cursor: 'pointer' }}>大小写不敏感</label>
            </Space>
          </Tooltip>
          <Tooltip title="只看大小/内容差异，修改时间不同不视为变更">
            <Space>
              <Switch
                size="small"
                checked={ignoreMtime}
                onChange={setIgnoreMtime}
                id="diff-ignore-mtime"
              />
              <label htmlFor="diff-ignore-mtime" style={{ cursor: 'pointer' }}>忽略修改时间</label>
            </Space>
          </Tooltip>
          <Tooltip title="只比较路径/mtime/内容差异，文件大小不同不视为变更">
            <Space>
              <Switch
                size="small"
                checked={ignoreSize}
                onChange={setIgnoreSize}
                id="diff-ignore-size"
              />
              <label htmlFor="diff-ignore-size" style={{ cursor: 'pointer' }}>忽略大小</label>
            </Space>
          </Tooltip>
          <Tooltip title="将 identical 条目写入对比库，大快照会显著增大对比库">
            <Space>
              <Switch
                size="small"
                checked={showIdentical}
                onChange={setShowIdentical}
                id="diff-show-identical"
              />
              <label htmlFor="diff-show-identical" style={{ cursor: 'pointer' }}>记录相同项</label>
            </Space>
          </Tooltip>
        </Space>
        {hash === 'sha256' && (
          <Typography.Text type="secondary" style={{ display: 'block', marginTop: 8 }}>
            需要先在两个快照上算好 sha256 哈希，才能识别改名与同尺寸改写
          </Typography.Text>
        )}
        {create.isError && (
          <Alert
            style={{ marginTop: 16 }}
            type="error"
            showIcon
            title={apiErrorDetail(create.error)}
          />
        )}
      </Card>

      <Card title="历史对比">
        {diffLoading ? (
          <Skeleton active />
        ) : historyItems.length ? (
          <>
            <ResizableTable<DiffRun>
              tableId="diff-history"
              rowKey="diff_id"
              size="small"
              tableLayout="fixed"
              pagination={false}
              scroll={{ x: 'max-content' }}
              dataSource={historyItems}
              columns={historyColumns}
            />
            <CursorPager
              pageSize={historySize}
              onPageSizeChange={changeHistorySize}
              canPrev={historyIndex > 0}
              onPrev={() => setCursorStack((s) => (s.length > 1 ? s.slice(0, -1) : s))}
              canNext={historyPage?.has_more ?? false}
              onNext={() => {
                if (historyPage?.next_cursor) {
                  setCursorStack((s) => [...s, historyPage.next_cursor!])
                }
              }}
              hint={`第 ${historyIndex + 1} 页 · 本页 ${historyItems.length} 条`}
            />
          </>
        ) : (
          <Empty description="暂无对比记录" />
        )}
      </Card>
    </div>
  )
}

function DiffDetail({ diffId }: { diffId: string }) {
  const [searchParams, setSearchParams] = useSearchParams()
  const { data: diff, isLoading: diffLoading, error: diffError } = useDiff(diffId)
  const { data: summary, isLoading: summaryLoading, error: summaryError } = useDiffSummary(diffId, 50)
  const [category, setCategory] = useState<string | undefined>(undefined)
  const [drawerEntry, setDrawerEntry] = useState<DiffEntry | null>(null)

  // 路径前缀筛选状态写进 URL（?prefix=…），便于分享与浏览器回退
  const prefix = searchParams.get('prefix') || ''
  const setPrefix = useCallback(
    (v: string) => {
      const next = new URLSearchParams(searchParams)
      if (v) next.set('prefix', v)
      else next.delete('prefix')
      setSearchParams(next)
    },
    [searchParams, setSearchParams]
  )

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
            <Space>
              <Text>证据等级：</Text>
              <EvidenceTag level={diff.evidence_level} evidence={diff.evidence} />
            </Space>
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
                tableLayout="fixed"
                pagination={false}
                scroll={{ y: 320 }}
                dataSource={summary.by_parent_dir}
                columns={[
                  { title: '目录', dataIndex: 'parent_dir', ellipsis: true, render: (v: string) => <EllipsisText value={v} code /> },
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
            {prefix.trim() && (
              <Tooltip title="路径前缀筛选已写入地址栏，可直接分享">
                <Tag color="geekblue" closable onClose={() => setPrefix('')}>
                  前缀 {prefix.trim()}
                </Tag>
              </Tooltip>
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
        <DiffEntriesTable
          diffId={diffId}
          category={category}
          pathPrefix={prefix}
          onPrefixChange={setPrefix}
          onRowClick={setDrawerEntry}
          aSnapshotId={diff?.a ?? ''}
          bSnapshotId={diff?.b ?? ''}
          aLabel={diff?.labels?.a || diff?.a || 'A'}
          bLabel={diff?.labels?.b || diff?.b || 'B'}
        />
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

type DiffSortKey = 'path' | 'size_delta'

function DiffEntriesTable({
  diffId,
  category,
  pathPrefix,
  onPrefixChange,
  onRowClick,
  aSnapshotId,
  bSnapshotId,
  aLabel,
  bLabel,
}: {
  diffId: string
  category: string | undefined
  /** URL 里的路径前缀（未 trim，可能为空串） */
  pathPrefix: string
  onPrefixChange: (v: string) => void
  onRowClick: (e: DiffEntry) => void
  aSnapshotId: string
  bSnapshotId: string
  aLabel: string
  bLabel: string
}) {
  const [pageSize, setPageSize] = useState(50)
  // cursor 栈分页：栈底 null = 第一页；上一页弹栈，下一页压入 next_cursor
  const [cursorStack, setCursorStack] = useState<(string | null)[]>([null])
  const [sort, setSort] = useState<DiffSortKey>('path')
  const [order, setOrder] = useState<'asc' | 'desc'>('asc')
  const rowMenu = useRowContextMenu()
  const prefixTimer = useRef<number | null>(null)
  const tableTopRef = useRef<HTMLDivElement | null>(null)
  const mountedRef = useRef(false)

  const pageIndex = cursorStack.length - 1
  const cursor = cursorStack[pageIndex]

  // 输入框本地值；URL 外部变化（回退/前进/清空标签）时在渲染期同步回输入框。
  // 防抖写在 onChange 里，400ms 后写回 URL。
  const [prefixInput, setPrefixInput] = useState(pathPrefix)
  const [syncedPrefix, setSyncedPrefix] = useState(pathPrefix)
  if (pathPrefix !== syncedPrefix) {
    setSyncedPrefix(pathPrefix)
    setPrefixInput(pathPrefix)
  }
  const handlePrefixInput = (v: string) => {
    setPrefixInput(v)
    if (prefixTimer.current) window.clearTimeout(prefixTimer.current)
    prefixTimer.current = window.setTimeout(() => onPrefixChange(v.trim()), 400)
  }

  const trimmedPrefix = pathPrefix.trim()

  const { data, isLoading, error } = useDiffEntries(diffId, {
    category,
    path_prefix: trimmedPrefix || undefined,
    limit: pageSize,
    sort,
    order,
    cursor: cursor ?? undefined,
  })
  const items = data?.items ?? []

  // 筛选条件变化 → 重置回第一页（渲染期同步，避免 effect 级联）
  const filterKey = `${category ?? ''}\u0000${trimmedPrefix}\u0000${sort}\u0000${order}`
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

  const goNext = () => {
    const nc = data?.next_cursor
    if (data?.has_more && nc) setCursorStack((s) => [...s, nc])
  }
  const goPrev = () => setCursorStack((s) => (s.length > 1 ? s.slice(0, -1) : s))
  const changePageSize = (n: number) => {
    setPageSize(n)
    setCursorStack([null])
  }

  const onTableChange: TableProps<DiffEntry>['onChange'] = (_pagination, _filters, sorter) => {
    const s = Array.isArray(sorter) ? sorter[0] : sorter
    if (!s || !s.columnKey || !s.order) return
    const key = s.columnKey as DiffSortKey
    const nextOrder = s.order === 'descend' ? 'desc' : 'asc'
    if (key === sort && nextOrder === order) return
    setSort(key)
    setOrder(nextOrder)
  }

  /** changed 类两侧都在：右键菜单给 A/B 两个打开项；added 只在 B、removed 只在 A。 */
  const rowTarget = (record: DiffEntry): RowContextTarget => {
    const bPath = record.b_path || record.path
    const aItem = { snapshotId: aSnapshotId, dir: dirNameOf(record.path), label: `在 A · ${aLabel} 中浏览` }
    const bItem = { snapshotId: bSnapshotId, dir: dirNameOf(bPath), label: `在 B · ${bLabel} 中浏览` }
    if (record.category === 'added') return { path: bPath, extraOpenItems: [bItem] }
    if (record.category === 'removed') return { path: record.path, extraOpenItems: [aItem] }
    if (
      record.category === 'type_changed' || record.category === 'size_changed' ||
      record.category === 'mtime_changed' || record.category === 'content_changed' ||
      record.category === 'moved_or_renamed' || record.category === 'identical'
    ) {
      return { path: record.path, extraOpenItems: [aItem, bItem] }
    }
    return { path: record.path, snapshotId: aSnapshotId, openDirPath: dirNameOf(record.path) }
  }

  const columns = [
    {
      title: '路径',
      dataIndex: 'path',
      key: 'path',
      ellipsis: true,
      sorter: true,
      sortOrder: sort === 'path' ? (order === 'asc' ? 'ascend' as const : 'descend' as const) : null,
      render: (v: string, record: DiffEntry) => (
        <Tooltip title={v} placement="topLeft" mouseEnterDelay={0.3}>
          <Button type="link" style={{ padding: 0, maxWidth: '100%' }} onClick={() => onRowClick(record)}>
            <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{v}</span>
          </Button>
        </Tooltip>
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
      title: '大小变化',
      dataIndex: 'size_delta',
      key: 'size_delta',
      width: 120,
      sorter: true,
      sortOrder: sort === 'size_delta' ? (order === 'asc' ? 'ascend' as const : 'descend' as const) : null,
      render: (v: number | null) => (
        <Text style={{ color: v == null || v === 0 ? undefined : v > 0 ? '#52c41a' : '#ff4d4f' }}>
          {v == null || v === 0 ? '—' : `${v > 0 ? '+' : ''}${formatFileSize(v)}`}
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
      <div ref={tableTopRef} />
      {error && <ErrorAlert error={error} />}
      <Space wrap style={{ marginBottom: 12 }} align="center">
        <Input
          prefix={<FilterOutlined style={{ color: '#bbb' }} />}
          placeholder="按路径前缀过滤，如 photos/2024（以 / 结尾表示目录）"
          style={{ width: 360 }}
          value={prefixInput}
          onChange={(e) => handlePrefixInput(e.target.value)}
        />
        {prefixInput && (
          <Button onClick={() => handlePrefixInput('')}>清空</Button>
        )}
        {trimmedPrefix && (
          <Text type="secondary">仅显示以 <Text code>{trimmedPrefix}</Text> 开头的路径</Text>
        )}
      </Space>
      <ResizableTable
        tableId="diff-entries"
        rowKey="id"
        size="small"
        loading={isLoading}
        tableLayout="fixed"
        columns={columns}
        dataSource={items}
        pagination={false}
        scroll={{ x: 'max-content' }}
        onChange={onTableChange}
        onRow={(record) => ({
          onContextMenu: (e) => rowMenu.open(e, rowTarget(record)),
        })}
        locale={{ emptyText: <Empty description="无差异文件" /> }}
      />
      <CursorPager
        pageSize={pageSize}
        onPageSizeChange={changePageSize}
        canPrev={pageIndex > 0}
        onPrev={goPrev}
        canNext={data?.has_more ?? false}
        onNext={goNext}
        hint={`第 ${pageIndex + 1} 页 · 本页 ${items.length} 条`}
      />
      {rowMenu.element}
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
        <div><Text style={labelStyle}>路径</Text>{entry.b_path || entry.path || '—'}</div>
        <div><Text style={labelStyle}>类型</Text>{entry.b_type || '—'}</div>
        <div><Text style={labelStyle}>大小</Text>{formatFileSize(entry.b_size)}</div>
        <div><Text style={labelStyle}>修改时间</Text>{nsToDate(entry.b_mtime_ns)}</div>
      </div>
    </Space>
  )
}
