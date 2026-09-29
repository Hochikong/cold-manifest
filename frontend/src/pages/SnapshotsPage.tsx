import { useEffect, useMemo, useState, useCallback } from 'react'
import { useSearchParams, useNavigate } from 'react-router-dom'
import {
  Card,
  Row,
  Col,
  Statistic,
  Tabs,
  Table,
  Tree,
  Breadcrumb,
  Button,
  Space,
  Input,
  Select,
  Typography,
  Empty,
  Spin,
  Skeleton,
  Alert,
  Drawer,
  Tooltip,
} from 'antd'
import {
  FolderOutlined,
  FileOutlined,
  ReloadOutlined,
  DownloadOutlined,
  SearchOutlined,
  ArrowLeftOutlined,
  HomeOutlined,
} from '@ant-design/icons'
import type { DataNode } from 'antd/es/tree'
import ReactECharts from 'echarts-for-react'
import {
  useSnapshots,
  useSnapshot,
  useSnapshotStats,
  useEntries,
  useTree,
  useDu,
  useSearch,
} from '../api/hooks'
import ErrorAlert from '../components/ErrorAlert'
import DuplicateReport from '../components/DuplicateReport'
import { formatFileSize, formatDateTime, formatNumber, nsToDate } from '../utils/format'
import { exportSnapshotUrl, type Entry, type TreeDir, type SearchItem } from '../api/client'

const { Title, Text } = Typography
const { Option } = Select

type SortKey = 'name' | 'size' | 'mtime'
type Order = 'asc' | 'desc'

const ENTRY_PAGE_SIZE = 200
const SEARCH_PAGE_SIZE = 200

function getSnapshotDisplayName(snapshot_id: string) {
  const parts = snapshot_id.split('/')
  return parts.length > 1 ? `${parts[0]} / ${parts[1]}` : snapshot_id
}

function sortArrow(order: Order) {
  return order === 'asc' ? '↑' : '↓'
}

export default function SnapshotsPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const navigate = useNavigate()
  const snapshotId = searchParams.get('snapshot') || undefined
  const activeTab = searchParams.get('tab') || 'overview'

  const { data: snapshots, isLoading: listLoading, error: listError } = useSnapshots()

  const setTab = (tab: string) => {
    const next = new URLSearchParams(searchParams)
    next.set('tab', tab)
    setSearchParams(next, { replace: true })
  }

  if (!snapshotId) {
    return (
      <SnapshotListView
        snapshots={snapshots?.items}
        loading={listLoading}
        error={listError}
        onSelect={(id) => {
          const next = new URLSearchParams()
          next.set('snapshot', id)
          next.set('tab', 'overview')
          setSearchParams(next)
        }}
      />
    )
  }

  return (
    <div>
      <Space align="center" style={{ marginBottom: 16 }}>
        <Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/snapshots')}>
          返回列表
        </Button>
        <Title level={4} style={{ margin: 0 }}>
          快照：{getSnapshotDisplayName(snapshotId)}
        </Title>
      </Space>
      <Tabs
        activeKey={activeTab}
        onChange={setTab}
        items={[
          { key: 'overview', label: '概览', children: <SnapshotOverview snapshotId={snapshotId} /> },
          { key: 'browse', label: '浏览', children: <DirectoryBrowser snapshotId={snapshotId} /> },
          { key: 'duplicates', label: '重复文件', children: <DuplicateReport snapshotId={snapshotId} /> },
          { key: 'search', label: '搜索', children: <SearchPanel snapshotId={snapshotId} /> },
          { key: 'export', label: '导出', children: <ExportPanel snapshotId={snapshotId} /> },
        ]}
      />
    </div>
  )
}

function SnapshotListView({
  snapshots,
  loading,
  error,
  onSelect,
}: {
  snapshots?: { snapshot_id: string; volume_id: string; collected_at: string; file_count: number; total_bytes: number }[]
  loading: boolean
  error: unknown
  onSelect: (id: string) => void
}) {
  const columns = [
    { title: '快照 ID', dataIndex: 'snapshot_id', key: 'snapshot_id', ellipsis: true },
    { title: '卷', dataIndex: 'volume_id', key: 'volume_id', ellipsis: true },
    { title: '采集时间', dataIndex: 'collected_at', key: 'collected_at', render: (v: string) => formatDateTime(v) },
    { title: '文件数', dataIndex: 'file_count', key: 'file_count', render: (v: number) => formatNumber(v) },
    { title: '总大小', dataIndex: 'total_bytes', key: 'total_bytes', render: (v: number) => formatFileSize(v) },
    {
      title: '操作',
      key: 'action',
      render: (_: unknown, record: { snapshot_id: string }) => (
        <Button type="primary" onClick={() => onSelect(record.snapshot_id)}>
          查看
        </Button>
      ),
    },
  ]

  return (
    <div>
      <Title level={4} style={{ marginTop: 0 }}>快照</Title>
      {!!error && <ErrorAlert error={error} />}
      {loading && <Spin style={{ display: 'block', margin: '32px auto' }} />}
      <Card>
        {snapshots?.length ? (
          <Table rowKey="snapshot_id" size="small" columns={columns} dataSource={snapshots} pagination={{ pageSize: 10 }} />
        ) : (
          <Empty description="暂无快照" />
        )}
      </Card>
    </div>
  )
}

function SnapshotOverview({ snapshotId }: { snapshotId: string }) {
  const { data: snapshot, isLoading: detailLoading, error: detailError } = useSnapshot(snapshotId)
  const { data: stats, isLoading: statsLoading, error: statsError } = useSnapshotStats(snapshotId)
  const { data: du, isLoading: duLoading, error: duError } = useDu(snapshotId, 0, 20)

  const error = detailError || statsError || duError

  const extChartOption = useMemo(() => {
    if (!stats?.ext_top_by_count.length) return null
    const items = stats.ext_top_by_count.slice(0, 10)
    return {
      tooltip: { trigger: 'axis' },
      grid: { left: 16, right: 16, top: 16, bottom: 8, containLabel: true },
      xAxis: { type: 'value' },
      yAxis: { type: 'category', data: items.map((i) => i.ext || '(无)').reverse() },
      series: [{ type: 'bar', data: items.map((i) => i.count).reverse(), itemStyle: { color: '#aa3bff' } }],
    }
  }, [stats])

  const sizeChartOption = useMemo(() => {
    if (!stats?.size_histogram.length) return null
    return {
      tooltip: { trigger: 'axis' },
      grid: { left: 16, right: 16, top: 16, bottom: 24, containLabel: true },
      xAxis: { type: 'category', data: stats.size_histogram.map((i) => i.label), axisLabel: { rotate: 30 } },
      yAxis: { type: 'value' },
      series: [{ type: 'bar', data: stats.size_histogram.map((i) => i.count), itemStyle: { color: '#1677ff' } }],
    }
  }, [stats])

  const depthChartOption = useMemo(() => {
    if (!stats?.depth_histogram.length) return null
    const sorted = [...stats.depth_histogram].sort((a, b) => a.depth - b.depth)
    return {
      tooltip: { trigger: 'axis' },
      grid: { left: 16, right: 16, top: 16, bottom: 24, containLabel: true },
      xAxis: { type: 'category', data: sorted.map((i) => `深度 ${i.depth}`) },
      yAxis: { type: 'value' },
      series: [{ type: 'bar', data: sorted.map((i) => i.count), itemStyle: { color: '#52c41a' } }],
    }
  }, [stats])

  return (
    <div>
      {error && <ErrorAlert error={error} />}

      <Row gutter={[16, 16]}>
        <Col xs={24} sm={12} md={6}>
          <Card>
            {detailLoading ? (
              <Skeleton active paragraph={{ rows: 2 }} />
            ) : (
              <Statistic title="文件总数" value={snapshot?.file_count ?? 0} />
            )}
          </Card>
        </Col>
        <Col xs={24} sm={12} md={6}>
          <Card>
            {detailLoading ? (
              <Skeleton active paragraph={{ rows: 2 }} />
            ) : (
              <Statistic title="目录总数" value={snapshot?.dir_count ?? 0} />
            )}
          </Card>
        </Col>
        <Col xs={24} sm={12} md={6}>
          <Card>
            {detailLoading ? (
              <Skeleton active paragraph={{ rows: 2 }} />
            ) : (
              <Statistic title="总逻辑大小" value={formatFileSize(snapshot?.total_bytes)} />
            )}
          </Card>
        </Col>
        <Col xs={24} sm={12} md={6}>
          <Card>
            {detailLoading ? (
              <Skeleton active paragraph={{ rows: 2 }} />
            ) : (
              <Statistic title="最深层级" value={snapshot?.max_depth ?? 0} suffix="级" />
            )}
          </Card>
        </Col>
      </Row>

      <Card title="元数据" style={{ marginTop: 16 }}>
        <Space orientation="vertical" style={{ width: '100%' }}>
          <Text>快照 ID：<Text code>{snapshot?.snapshot_id}</Text></Text>
          <Text>卷 ID：<Text code>{snapshot?.volume_id}</Text></Text>
          <Text>采集时间：{formatDateTime(snapshot?.collected_at)}</Text>
          <Text>哈希策略：{snapshot?.hash_policy}</Text>
          <Text>跳过项：{formatNumber(snapshot?.skipped_count)}</Text>
        </Space>
      </Card>

      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col xs={24} md={12}>
          <Card title="扩展名 Top 10（按数量）">
            {statsLoading ? (
              <Skeleton active paragraph={{ rows: 5 }} />
            ) : extChartOption ? (
              <ReactECharts option={extChartOption} style={{ height: 280 }} />
            ) : (
              <Empty description="暂无数据" />
            )}
          </Card>
        </Col>
        <Col xs={24} md={12}>
          <Card title="文件大小分布">
            {statsLoading ? (
              <Skeleton active paragraph={{ rows: 5 }} />
            ) : sizeChartOption ? (
              <ReactECharts option={sizeChartOption} style={{ height: 280 }} />
            ) : (
              <Empty description="暂无数据" />
            )}
          </Card>
        </Col>
      </Row>

      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col xs={24} md={12}>
          <Card title="深度分布">
            {statsLoading ? (
              <Skeleton active paragraph={{ rows: 5 }} />
            ) : depthChartOption ? (
              <ReactECharts option={depthChartOption} style={{ height: 280 }} />
            ) : (
              <Empty description="暂无数据" />
            )}
          </Card>
        </Col>
        <Col xs={24} md={12}>
          <Card title="顶层目录占用 Top">
            {duLoading ? (
              <Skeleton active />
            ) : du?.items.length ? (
              <Table
                rowKey="entry_id"
                size="small"
                pagination={false}
                columns={[
                  { title: '名称', dataIndex: 'name', ellipsis: true },
                  { title: '大小', dataIndex: 'total_bytes', render: (v: number) => formatFileSize(v), width: 120 },
                ]}
                dataSource={du.items}
              />
            ) : (
              <Empty description="暂无数据" />
            )}
          </Card>
        </Col>
      </Row>

      <Card title="最大文件 Top 50" style={{ marginTop: 16 }}>
        {statsLoading ? (
          <Skeleton active />
        ) : stats?.top_files.length ? (
          <Table
            rowKey="entry_id"
            size="small"
            pagination={false}
            scroll={{ x: 'max-content' }}
            columns={[
              { title: '名称', dataIndex: 'name', ellipsis: true },
              { title: '路径', dataIndex: 'path', ellipsis: true },
              { title: '大小', dataIndex: 'size_bytes', render: (v: number) => formatFileSize(v), width: 120 },
              { title: '修改时间', dataIndex: 'mtime_ns', render: (v: string | null) => nsToDate(v), width: 160 },
            ]}
            dataSource={stats.top_files}
          />
        ) : (
          <Empty description="暂无数据" />
        )}
      </Card>
    </div>
  )
}

interface TreeNodeData extends DataNode {
  entryId: number
  isLeaf?: boolean
}

function DirectoryBrowser({ snapshotId }: { snapshotId: string }) {
  const [searchParams, setSearchParams] = useSearchParams()
  const parentIdParam = Number(searchParams.get('parent_id') || '0')
  const [parentId, setParentId] = useState<number>(parentIdParam)
  const [sort, setSort] = useState<SortKey>('name')
  const [order, setOrder] = useState<Order>('asc')
  const [typeFilter, setTypeFilter] = useState<string | undefined>(undefined)
  const [allItems, setAllItems] = useState<Entry[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [loadingMore, setLoadingMore] = useState(false)
  const [drawerEntry, setDrawerEntry] = useState<Entry | null>(null)
  const [treeData, setTreeData] = useState<TreeNodeData[]>([])
  const [expandedKeys, setExpandedKeys] = useState<React.Key[]>([])

  useEffect(() => {
    setParentId(parentIdParam)
  }, [parentIdParam])

  const { data: treeRoot, isLoading: treeLoading } = useTree(snapshotId, 0)
  const { data: entriesRes, isLoading: entriesLoading, error: entriesError } = useEntries(snapshotId, {
    parent_id: parentId,
    limit: ENTRY_PAGE_SIZE,
    sort,
    order,
    type: typeFilter as any,
  })

  useEffect(() => {
    if (entriesRes) {
      setAllItems(entriesRes.items)
      setCursor(entriesRes.next_cursor)
    }
  }, [entriesRes])

  useEffect(() => {
    if (treeRoot?.dirs) {
      setTreeData(
        treeRoot.dirs.map((d) => ({
          key: d.entry_id,
          title: d.name,
          entryId: d.entry_id,
          isLeaf: d.dir_count === 0,
          icon: <FolderOutlined />,
        }))
      )
    }
  }, [treeRoot])

  const loadTreeChildren = useCallback(
    async (node: TreeNodeData) => {
      const res = await fetch(`/api/snapshots/${encodeURIComponent(snapshotId)}/tree?parent_id=${node.entryId}`)
      const data = await res.json()
      const children: TreeNodeData[] = data.dirs.map((d: TreeDir) => ({
        key: d.entry_id,
        title: d.name,
        entryId: d.entry_id,
        isLeaf: d.dir_count === 0,
        icon: <FolderOutlined />,
      }))
      setTreeData((prev) => updateTreeChildren(prev, node.key, children))
    },
    [snapshotId]
  )

  const onSelectTree = (_: React.Key[], info: { node: TreeNodeData }) => {
    const next = new URLSearchParams(searchParams)
    next.set('parent_id', String(info.node.entryId))
    setSearchParams(next, { replace: true })
  }

  const loadMore = async () => {
    if (!cursor) return
    setLoadingMore(true)
    const res = await fetch(
      `/api/snapshots/${encodeURIComponent(snapshotId)}/entries?parent_id=${parentId}&cursor=${encodeURIComponent(cursor)}&limit=${ENTRY_PAGE_SIZE}&sort=${sort}&order=${order}${typeFilter ? `&type=${typeFilter}` : ''}`
    )
    const data = await res.json()
    setAllItems((prev) => [...prev, ...data.items])
    setCursor(data.next_cursor)
    setLoadingMore(false)
  }

  const toggleSort = (key: SortKey) => {
    if (sort === key) {
      setOrder(order === 'asc' ? 'desc' : 'asc')
    } else {
      setSort(key)
      setOrder('asc')
    }
  }

  const columns = [
    {
      title: '名称',
      dataIndex: 'name',
      key: 'name',
      render: (_: string, record: Entry) => (
        <Space>
          {record.type === 'dir' ? <FolderOutlined /> : <FileOutlined />}
          <Button
            type="link"
            style={{ padding: 0 }}
            onClick={() => {
              if (record.type === 'dir') {
                const next = new URLSearchParams(searchParams)
                next.set('parent_id', String(record.entry_id))
                setSearchParams(next, { replace: true })
              } else {
                setDrawerEntry(record)
              }
            }}
          >
            {record.name}
          </Button>
        </Space>
      ),
    },
    {
      title: '类型',
      dataIndex: 'type',
      key: 'type',
      width: 80,
      render: (v: string) => (v === 'dir' ? '目录' : v === 'file' ? '文件' : v),
    },
    {
      title: `大小 ${sort === 'size' ? sortArrow(order) : ''}`,
      key: 'size',
      width: 140,
      render: (_: unknown, record: Entry) => {
        if (record.type === 'dir' && record.rollup) {
          return (
            <Tooltip title={`递归：${formatFileSize(record.rollup.total_bytes)}`}>
              <Text>{formatFileSize(record.rollup.total_bytes)}</Text>
            </Tooltip>
          )
        }
        return formatFileSize(record.size_bytes)
      },
      onHeaderCell: () => ({ onClick: () => toggleSort('size') }),
    },
    {
      title: `修改时间 ${sort === 'mtime' ? sortArrow(order) : ''}`,
      key: 'mtime',
      width: 170,
      render: (_: unknown, record: Entry) => nsToDate(record.mtime_ns),
      onHeaderCell: () => ({ onClick: () => toggleSort('mtime') }),
    },
    {
      title: '扩展名',
      dataIndex: 'ext',
      key: 'ext',
      width: 100,
      render: (v: string) => v || '-',
    },
  ]

  return (
    <div>
      {entriesError && <ErrorAlert error={entriesError} />}
      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col xs={24} md={12}>
          <Breadcrumb
            items={[
              {
                title: (
                  <Button
                    type="link"
                    icon={<HomeOutlined />}
                    style={{ padding: 0 }}
                    onClick={() => {
                      const next = new URLSearchParams(searchParams)
                      next.set('parent_id', '0')
                      setSearchParams(next, { replace: true })
                    }}
                  >
                    根目录
                  </Button>
                ),
              },
              { title: `当前：${entriesRes?.parent_path || '.'}` },
            ]}
          />
        </Col>
        <Col xs={24} md={12}>
          <Space wrap>
            <Button icon={<ReloadOutlined />} onClick={() => window.location.reload()}>刷新</Button>
            <Select
              placeholder="类型过滤"
              allowClear
              style={{ width: 120 }}
              value={typeFilter}
              onChange={(v) => setTypeFilter(v)}
            >
              <Option value="file">文件</Option>
              <Option value="dir">目录</Option>
            </Select>
            <Text type="secondary">
              直接子项：文件 {formatNumber(entriesRes?.direct_child_counts.files)} / 目录 {formatNumber(entriesRes?.direct_child_counts.dirs)}
            </Text>
          </Space>
        </Col>
      </Row>

      <Row gutter={[16, 16]}>
        <Col xs={24} md={7} lg={5}>
          <Card title="目录树" styles={{ body: { padding: 12, maxHeight: 600, overflow: 'auto' } }}>
            {treeLoading ? (
              <Spin />
            ) : (
              <DirectoryTree
                treeData={treeData}
                loadData={loadTreeChildren as any}
                onSelect={onSelectTree as any}
                expandedKeys={expandedKeys}
                onExpand={(keys) => setExpandedKeys(keys)}
              />
            )}
          </Card>
        </Col>
        <Col xs={24} md={17} lg={19}>
          <Card>
            <Table
              rowKey="entry_id"
              size="small"
              loading={entriesLoading}
              columns={columns}
              dataSource={allItems}
              pagination={false}
              scroll={{ x: 'max-content' }}
              locale={{ emptyText: <Empty description="空目录" /> }}
            />
            {cursor && (
              <div style={{ textAlign: 'center', marginTop: 16 }}>
                <Button loading={loadingMore} onClick={loadMore}>
                  加载更多（剩余未加载）
                </Button>
              </div>
            )}
          </Card>
        </Col>
      </Row>

      <Drawer
        title={drawerEntry?.name}
        open={!!drawerEntry}
        onClose={() => setDrawerEntry(null)}
        styles={{ wrapper: { width: 480 } }}
      >
        {drawerEntry && (
          <Space orientation="vertical" style={{ width: '100%' }}>
            <Text>类型：{drawerEntry.type}</Text>
            <Text>大小：{formatFileSize(drawerEntry.size_bytes)}</Text>
            <Text>修改时间：{nsToDate(drawerEntry.mtime_ns)}</Text>
            <Text>扩展名：{drawerEntry.ext || '-'}</Text>
            <Text>哈希状态：{drawerEntry.hash_state || '未计算'}</Text>
          </Space>
        )}
      </Drawer>
    </div>
  )
}

function DirectoryTree(props: React.ComponentProps<typeof Tree>) {
  return <Tree.DirectoryTree {...props} />
}

function updateTreeChildren(nodes: TreeNodeData[], key: React.Key, children: TreeNodeData[]): TreeNodeData[] {
  return nodes.map((node) => {
    if (node.key === key) {
      return { ...node, children }
    }
    if (node.children) {
      return { ...node, children: updateTreeChildren(node.children as TreeNodeData[], key, children) }
    }
    return node
  })
}

type SearchMode = 'prefix' | 'fulltext'

const MODE_LABELS: Record<SearchMode, string> = {
  prefix: '前缀',
  fulltext: '全文',
}

function SearchPanel({ snapshotId }: { snapshotId: string }) {
  const [q, setQ] = useState('')
  const [mode, setMode] = useState<SearchMode>('prefix')
  const [typeFilter, setTypeFilter] = useState<string | undefined>(undefined)
  const [ext, setExt] = useState('')
  const [results, setResults] = useState<SearchItem[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [loadingMore, setLoadingMore] = useState(false)
  const [hasSearched, setHasSearched] = useState(false)
  const [fulltextAvailable, setFulltextAvailable] = useState<boolean | null>(null)

  // 轻量探测当前快照是否已建全文索引，仅用于禁用/提示
  useEffect(() => {
    let cancelled = false
    setFulltextAvailable(null)
    fetch(`/api/snapshots/${encodeURIComponent(snapshotId)}/search?q=%20&mode=prefix&limit=1`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (!cancelled && data && typeof data.fulltext_available === 'boolean') {
          setFulltextAvailable(data.fulltext_available)
        }
      })
      .catch(() => {
        // 探测失败时不阻塞搜索
      })
    return () => {
      cancelled = true
    }
  }, [snapshotId])

  // 如果当前选的是全文但快照无索引，自动切回前缀
  useEffect(() => {
    if (mode === 'fulltext' && fulltextAvailable === false) {
      setMode('prefix')
    }
  }, [mode, fulltextAvailable])

  const canSearch = !!q && !(mode === 'fulltext' && q.length < 3)

  const { data: searchRes, isLoading, error, refetch } = useSearch(snapshotId, {
    q,
    mode,
    limit: SEARCH_PAGE_SIZE,
    type: typeFilter as any,
    ext: ext || undefined,
  })

  useEffect(() => {
    if (searchRes) {
      setResults(searchRes.items)
      setCursor(searchRes.next_cursor)
      if (typeof searchRes.fulltext_available === 'boolean') {
        setFulltextAvailable(searchRes.fulltext_available)
      }
    }
  }, [searchRes])

  const doSearch = () => {
    setHasSearched(true)
    refetch()
  }

  const loadMore = async () => {
    if (!cursor) return
    setLoadingMore(true)
    const res = await fetch(
      `/api/snapshots/${encodeURIComponent(snapshotId)}/search?q=${encodeURIComponent(q)}&mode=${mode}&cursor=${encodeURIComponent(cursor)}&limit=${SEARCH_PAGE_SIZE}${typeFilter ? `&type=${typeFilter}` : ''}${ext ? `&ext=${encodeURIComponent(ext)}` : ''}`
    )
    const data = await res.json()
    setResults((prev) => [...prev, ...data.items])
    setCursor(data.next_cursor)
    setLoadingMore(false)
  }

  const columns = [
    { title: '名称', dataIndex: 'name', ellipsis: true },
    { title: '路径', dataIndex: 'path', ellipsis: true },
    { title: '类型', dataIndex: 'type', width: 80, render: (v: string) => (v === 'dir' ? '目录' : v === 'file' ? '文件' : v) },
    { title: '大小', dataIndex: 'size_bytes', width: 120, render: (v: number | null) => formatFileSize(v) },
    { title: '扩展名', dataIndex: 'ext', width: 100, render: (v: string) => v || '-' },
  ]

  const placeholder = mode === 'fulltext' ? '输入关键词（至少 3 个字符）…' : '输入关键词前缀…'
  const infoDescription =
    mode === 'fulltext'
      ? '当前为全文搜索模式，命中路径或名称中包含关键词的条目。'
      : '当前为前缀搜索模式，名称以输入关键词开头的条目会被命中。'

  return (
    <div>
      {error && <ErrorAlert error={error} />}
      <Card style={{ marginBottom: 16 }}>
        <Space wrap align="start">
          <Input
            placeholder={placeholder}
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onPressEnter={doSearch}
            prefix={<SearchOutlined />}
            style={{ width: 280 }}
          />
          <Select value={mode} onChange={(v) => setMode(v as SearchMode)} style={{ width: 120 }}>
            <Option value="prefix">{MODE_LABELS.prefix}</Option>
            <Option value="fulltext" disabled={fulltextAvailable === false}>
              {MODE_LABELS.fulltext}
            </Option>
          </Select>
          <Select placeholder="类型" allowClear style={{ width: 120 }} value={typeFilter} onChange={setTypeFilter}>
            <Option value="file">文件</Option>
            <Option value="dir">目录</Option>
          </Select>
          <Input placeholder="扩展名，如 txt" value={ext} onChange={(e) => setExt(e.target.value)} style={{ width: 140 }} />
          <Button
            type="primary"
            icon={<SearchOutlined />}
            onClick={doSearch}
            loading={isLoading}
            disabled={!canSearch}
          >
            搜索
          </Button>
        </Space>

        {fulltextAvailable === false && (
          <Alert
            style={{ marginTop: 16, marginBottom: 0 }}
            type="warning"
            showIcon
            title="该快照未构建全文索引，可用 CLDM 命令 build-fts 补建"
          />
        )}
        {mode === 'fulltext' && q.length > 0 && q.length < 3 && (
          <Alert
            style={{ marginTop: 16, marginBottom: 0 }}
            type="warning"
            showIcon
            title="全文模式至少 3 个字符"
          />
        )}
      </Card>

      {hasSearched && !isLoading && (
        <Card title={`搜索结果（${formatNumber(results.length)} 条）`}>
          {results.length ? (
            <>
              <Table rowKey="entry_id" size="small" columns={columns} dataSource={results} pagination={false} scroll={{ x: 'max-content' }} />
              {cursor && (
                <div style={{ textAlign: 'center', marginTop: 16 }}>
                  <Button loading={loadingMore} onClick={loadMore}>加载更多</Button>
                </div>
              )}
            </>
          ) : (
            <Empty description="无结果，尝试放宽条件" />
          )}
        </Card>
      )}

      {!hasSearched && (
        <Alert
          type="info"
          showIcon
          title="输入关键词或选择筛选条件开始搜索"
          description={infoDescription}
        />
      )}
    </div>
  )
}

function ExportPanel({ snapshotId }: { snapshotId: string }) {
  return (
    <Card title="导出快照数据">
      <Space orientation="vertical" style={{ width: '100%' }}>
        <Alert
          type="info"
          showIcon
          title="导出格式说明"
          description="CSV：entries 全表；V1 CSV：兼容旧版的三件套 zip。"
        />
        <Space>
          <Button icon={<DownloadOutlined />} href={exportSnapshotUrl(snapshotId, 'csv')}>
            导出 CSV
          </Button>
          <Button icon={<DownloadOutlined />} href={exportSnapshotUrl(snapshotId, 'v1_csv')}>
            导出 V1 CSV（zip）
          </Button>
        </Space>
      </Space>
    </Card>
  )
}
