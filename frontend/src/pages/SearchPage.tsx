import { useMemo, useState } from 'react'
import { useSearchParams, useNavigate } from 'react-router-dom'
import {
  Alert,
  Card,
  Empty,
  Input,
  Segmented,
  Select,
  Space,
  Spin,
  Table,
  Typography,
  Button,
} from 'antd'
import type { TableProps } from 'antd'
import { FileOutlined, FolderOutlined, SearchOutlined } from '@ant-design/icons'
import { useGlobalSearch, useSnapshots, useVolumes } from '../api/hooks'
import ErrorAlert from '../components/ErrorAlert'
import { useRowContextMenu } from '../hooks/useRowContextMenu'
import { formatDateTime, formatFileSize, formatNumber, nsToDate } from '../utils/format'
import { dirNameOf, snapshotBrowseUrl } from '../utils/path'
import { globalSearch, type GlobalSearchItem, type GlobalSearchParams } from '../api/client'

const { Title, Text } = Typography
const PAGE_SIZE = 50
const MIN_CHARS = 2

type SearchMode = 'prefix' | 'fulltext'

function shortSnapshotId(snapshot_id: string): string {
  const parts = snapshot_id.split('/')
  return parts.length > 1 ? parts[1] : snapshot_id
}

export default function SearchPage() {
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const rowMenu = useRowContextMenu()

  const qParam = searchParams.get('q') ?? ''
  const mode: SearchMode = searchParams.get('mode') === 'fulltext' ? 'fulltext' : 'prefix'
  const volumeId = searchParams.get('volume_id') ?? undefined
  const snapshotId = searchParams.get('snapshot_id') ?? undefined
  const typeParam = searchParams.get('type')
  const type: 'file' | 'dir' | undefined = typeParam === 'file' || typeParam === 'dir' ? typeParam : undefined

  const [draft, setDraft] = useState(qParam)
  // URL 上的 q 被外部改变（如顶栏搜索跳转）时，同步草稿；渲染期条件 setState 是 React 官方推荐写法
  const [prevQ, setPrevQ] = useState(qParam)
  if (qParam !== prevQ) {
    setPrevQ(qParam)
    setDraft(qParam)
  }

  const setParam = (key: string, value: string | undefined) => {
    const next = new URLSearchParams(searchParams)
    if (value) next.set(key, value)
    else next.delete(key)
    setSearchParams(next, { replace: true })
  }

  const submit = () => {
    const q = draft.trim()
    if (q.length < MIN_CHARS) return
    setParam('q', q)
  }

  const params: GlobalSearchParams = useMemo(
    () => ({ q: qParam, mode, limit: PAGE_SIZE, volume_id: volumeId, snapshot_id: snapshotId, type }),
    [qParam, mode, volumeId, snapshotId, type],
  )
  const active = qParam.trim().length >= MIN_CHARS
  const { data, isFetching, error } = useGlobalSearch(params, active)

  // 「加载更多」追加页：keyed 到请求签名，筛选变化自动失效（与 DuplicateReport 同一套路）
  const requestKey = `${qParam}|${mode}|${volumeId ?? ''}|${snapshotId ?? ''}|${type ?? ''}`
  const [extra, setExtra] = useState<{ key: string; items: GlobalSearchItem[]; cursor: string | null }>({
    key: '',
    items: [],
    cursor: null,
  })
  const extraMatches = extra.key === requestKey
  const [loadMorePending, setLoadMorePending] = useState(false)
  const items = useMemo(
    () => (extraMatches ? [...(data?.items ?? []), ...extra.items] : (data?.items ?? [])),
    [extraMatches, data, extra],
  )
  const cursor = extraMatches ? extra.cursor : (data?.next_cursor ?? null)
  const scanned = data?.scanned ?? null

  const loadMore = async () => {
    if (!cursor) return
    setLoadMorePending(true)
    try {
      const res = await globalSearch({ ...params, cursor })
      setExtra({
        key: requestKey,
        items: [...(extraMatches ? extra.items : []), ...res.items],
        cursor: res.next_cursor,
      })
    } finally {
      setLoadMorePending(false)
    }
  }

  const { data: volumes } = useVolumes()
  const { data: snapshots } = useSnapshots()
  const volumeOptions = useMemo(
    () => (volumes?.items ?? []).map((v) => ({ value: v.volume_id, label: v.volume_id })).sort((a, b) => a.value.localeCompare(b.value)),
    [volumes],
  )
  const snapshotOptions = useMemo(
    () =>
      (snapshots?.items ?? [])
        .map((s) => ({ value: s.snapshot_id, label: `${s.volume_id} / ${shortSnapshotId(s.snapshot_id)}` }))
        .sort((a, b) => a.value.localeCompare(b.value)),
    [snapshots],
  )

  const columns: TableProps<GlobalSearchItem>['columns'] = [
    {
      title: '名称',
      dataIndex: 'name',
      key: 'name',
      width: 180,
      ellipsis: true,
      render: (_: string, record: GlobalSearchItem) => (
        <Space size={6}>
          {record.type === 'dir' ? <FolderOutlined style={{ color: '#8c8c8c' }} /> : <FileOutlined style={{ color: '#8c8c8c' }} />}
          <Text strong>{record.name}</Text>
        </Space>
      ),
    },
    { title: '路径', dataIndex: 'path', key: 'path', ellipsis: true },
    { title: '类型', dataIndex: 'type', key: 'type', width: 80, render: (v: string) => (v === 'dir' ? '目录' : v === 'file' ? '文件' : v) },
    { title: '大小', dataIndex: 'size_bytes', key: 'size_bytes', width: 110, align: 'right', render: (v: number | null) => formatFileSize(v) },
    { title: '修改时间', dataIndex: 'mtime_ns', key: 'mtime_ns', width: 150, render: (v: number | null) => nsToDate(v != null ? String(v) : null) },
    {
      title: '快照',
      key: 'snapshot',
      width: 240,
      ellipsis: true,
      render: (_: unknown, record: GlobalSearchItem) => (
        <Space size={6}>
          <Text code style={{ fontSize: 12 }}>{shortSnapshotId(record.snapshot_id)}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>{formatDateTime(record.collected_at)}</Text>
        </Space>
      ),
    },
  ]

  // 后端 /api/search 按固定口径排序（快照新旧 + 路径），无 sort 参数，故不提供表头排序
  const rowTarget = (record: GlobalSearchItem) => ({
    path: record.path,
    name: record.name,
    snapshotId: record.snapshot_id,
    openDirPath: record.type === 'dir' ? record.path : dirNameOf(record.path),
  })

  const fulltextFellBack = mode === 'fulltext' && scanned != null && !scanned.fulltext_used && items.length > 0

  return (
    <div>
      <Space align="center" style={{ marginBottom: 16 }} wrap>
        <Title level={4} style={{ margin: 0 }}>全局搜索</Title>
        <Text type="secondary">跨快照搜索文件与目录（按快照新旧 + 路径排序，最多搜索最近 20 个已封存快照）</Text>
      </Space>

      <Card style={{ marginBottom: 16 }}>
        <Space wrap align="center">
          <Input
            placeholder="输入关键词（至少 2 个字符，回车搜索）"
            prefix={<SearchOutlined />}
            allowClear
            style={{ width: 320 }}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onPressEnter={submit}
            autoFocus
          />
          <Segmented<SearchMode>
            value={mode}
            onChange={(v) => setParam('mode', v === 'fulltext' ? 'fulltext' : undefined)}
            options={[
              { label: '前缀', value: 'prefix' },
              { label: '全文', value: 'fulltext' },
            ]}
          />
          <Select
            placeholder="按卷筛选"
            allowClear
            showSearch
            optionFilterProp="label"
            style={{ minWidth: 180 }}
            value={volumeId}
            onChange={(v) => setParam('volume_id', v)}
            options={volumeOptions}
          />
          <Select
            placeholder="按快照筛选"
            allowClear
            showSearch
            optionFilterProp="label"
            style={{ minWidth: 220 }}
            value={snapshotId}
            onChange={(v) => setParam('snapshot_id', v)}
            options={snapshotOptions}
          />
          <Select
            placeholder="类型"
            allowClear
            style={{ width: 110 }}
            value={type}
            onChange={(v) => setParam('type', v)}
            options={[
              { value: 'file', label: '文件' },
              { value: 'dir', label: '目录' },
            ]}
          />
        </Space>
        {mode === 'fulltext' && (
          <Text type="secondary" style={{ display: 'block', marginTop: 8, fontSize: 12 }}>
            全文模式命中路径或名称中包含关键词的条目；未建全文索引的快照会自动回退为前缀匹配。
          </Text>
        )}
      </Card>

      {error && <ErrorAlert error={error} />}

      {fulltextFellBack && (
        <Alert
          style={{ marginBottom: 16 }}
          type="info"
          showIcon
          title="所选快照均未建立全文索引，本次结果为前缀匹配（名称以关键词开头）"
        />
      )}

      {!active ? (
        <Empty description="输入至少 2 个字符开始搜索" style={{ marginTop: 64 }} />
      ) : isFetching && items.length === 0 ? (
        <Spin style={{ display: 'block', margin: '48px auto' }} />
      ) : items.length ? (
        <Card
          title={`搜索结果（已加载 ${formatNumber(items.length)} 条）`}
          extra={scanned && <Text type="secondary">已搜 {formatNumber(scanned.snapshots)} 个快照</Text>}
        >
          <Table<GlobalSearchItem>
            rowKey={(r) => `${r.snapshot_id}:${r.entry_id}`}
            size="small"
            tableLayout="fixed"
            columns={columns}
            dataSource={items}
            pagination={false}
            loading={isFetching && items.length > 0}
            onRow={(record) => ({
              onClick: () => navigate(snapshotBrowseUrl(record.snapshot_id, record.type === 'dir' ? record.path : dirNameOf(record.path))),
              onContextMenu: (e) => rowMenu.open(e, rowTarget(record)),
              style: { cursor: 'pointer' },
            })}
          />
          {cursor && (
            <div style={{ textAlign: 'center', marginTop: 16 }}>
              <Space>
                {scanned && (
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    已搜 {formatNumber(scanned.snapshots)} 个快照
                  </Text>
                )}
                <Spin spinning={loadMorePending || isFetching}>
                  <Button onClick={() => void loadMore()}>加载更多</Button>
                </Spin>
              </Space>
            </div>
          )}
        </Card>
      ) : (
        !isFetching && (
          <Card>
            <Empty description={`没有匹配「${qParam}」的结果，尝试换个关键词或放宽筛选`} />
          </Card>
        )
      )}
      {rowMenu.element}
    </div>
  )
}
