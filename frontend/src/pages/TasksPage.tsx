import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Card, Button, Tag, Progress, Space, Typography, Empty, Spin, Modal, Drawer, Select, Tooltip } from 'antd'
import { ReloadOutlined, StopOutlined, ApartmentOutlined } from '@ant-design/icons'
import { useTasks, useCancelTask, useBatch } from '../api/hooks'
import { listTasks, type Task, type Batch, type TaskStatus } from '../api/client'
import ErrorAlert from '../components/ErrorAlert'
import EllipsisText from '../components/EllipsisText'
import { useShowApiError } from '../utils/apiError'
import { formatTaskMessage, formatTaskStatus } from '../utils/taskMessage'
import { formatDateTime } from '../utils/format'
import ResizableTable from '../components/ResizableTable'

const { Text } = Typography
const PAGE_SIZE = 20

/** 后端 GET /api/tasks?status= 只接受单个状态（且不含 cancelling）；多选由前端并行请求合并。 */
const FILTERABLE_STATUSES: TaskStatus[] = ['pending', 'running', 'done', 'error', 'cancelled']

const statusMap: Record<Task['status'], { label: string; color: string }> = {
  pending: { label: '待处理', color: 'default' },
  running: { label: '运行中', color: 'processing' },
  cancelling: { label: '取消中', color: 'processing' },
  cancelled: { label: '已取消', color: 'warning' },
  done: { label: '完成', color: 'success' },
  error: { label: '失败', color: 'error' },
}

const typeMap: Record<Task['type'], string> = {
  import: '导入',
  collect: '采集',
  hash: '哈希',
  diff: '对比',
  build_fts: '全文索引',
  build_stats: '统计缓存',
}

const batchStatusMap: Record<Batch['status'], { label: string; color: string }> = {
  running: { label: '运行中', color: 'processing' },
  done: { label: '完成', color: 'success' },
  partial: { label: '部分完成', color: 'warning' },
}

function isActiveStatus(t: Task): boolean {
  return t.status === 'pending' || t.status === 'running' || t.status === 'cancelling'
}

function byCreatedDesc(a: Task, b: Task): number {
  return (b.created_at ?? '').localeCompare(a.created_at ?? '')
}

interface StatusCursor {
  cursor: string | null
  hasMore: boolean
}

export default function TasksPage() {
  const navigate = useNavigate()
  const [items, setItems] = useState<Task[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [loadingMore, setLoadingMore] = useState(false)
  const [selectedBatchId, setSelectedBatchId] = useState<string | null>(null)

  const [statusFilter, setStatusFilter] = useState<TaskStatus[]>([])
  const [typeFilter, setTypeFilter] = useState<Task['type'] | undefined>(undefined)
  const multiMode = statusFilter.length >= 2

  // 单请求模式（未筛选 / 单状态）：走 useTasks，活跃任务 4s 轮询
  const { data, isLoading, error: singleError, refetch } = useTasks({ limit: PAGE_SIZE, status: statusFilter[0] }, !multiMode)

  // 多状态并行合并
  const [multiLoading, setMultiLoading] = useState(false)
  const [multiError, setMultiError] = useState<unknown>(null)
  const [cursors, setCursors] = useState<Record<string, StatusCursor>>({})
  const hasPagedRef = useRef(false)
  const hasActiveRef = useRef(false)

  useEffect(() => {
    hasActiveRef.current = multiMode && items.some(isActiveStatus)
  }, [multiMode, items])

  /** 清空累积，从头拉（筛选变化时由 onChange 调用）。 */
  const resetPaging = useCallback(() => {
    setItems([])
    setCursor(null)
    setCursors({})
    setMultiError(null)
    hasPagedRef.current = false
  }, [])

  const applyStatusFilter = (v: TaskStatus[]) => {
    setStatusFilter(v)
    resetPaging()
  }

  /** 拉一页（每状态各一页）并按创建时间倒序合并；append=false 时保留旧尾部去重（轮询不丢已加载页）。 */
  const fetchMultiPage = useCallback(
    async (cursorMap: Record<string, string | undefined>, append: boolean) => {
      setMultiLoading(true)
      try {
        const results = await Promise.all(
          statusFilter.map(async (s) => {
            const res = await listTasks({ limit: PAGE_SIZE, status: s, cursor: cursorMap[s] })
            return [s, res] as const
          }),
        )
        const fresh = results.flatMap(([, r]) => r.items)
        setItems((prev) => {
          const seen = new Set(fresh.map((t) => t.id))
          const base = prev.filter((t) => !seen.has(t.id))
          const merged = append ? [...base, ...fresh] : [...fresh, ...base]
          return merged.sort(byCreatedDesc)
        })
        setCursors(
          Object.fromEntries(results.map(([s, r]) => [s, { cursor: r.next_cursor, hasMore: r.has_more }])),
        )
        setMultiError(null)
      } catch (e) {
        setMultiError(e)
      } finally {
        setMultiLoading(false)
      }
    },
    [statusFilter],
  )

  // 多状态模式：首拉（延后一拍，避免 effect 内同步 setState）+ 活跃任务轮询
  useEffect(() => {
    if (!multiMode) return
    const first = window.setTimeout(() => {
      void fetchMultiPage({}, false)
    }, 0)
    const timer = window.setInterval(() => {
      if (hasActiveRef.current) void fetchMultiPage({}, false)
    }, 4_000)
    return () => {
      window.clearTimeout(first)
      window.clearInterval(timer)
    }
  }, [multiMode, fetchMultiPage])

  // 单请求模式：合并刷新数据（保留已翻页尾部）
  useEffect(() => {
    if (multiMode || !data) return
    if (!hasPagedRef.current) {
      setItems(data.items)
      setCursor(data.next_cursor)
    } else {
      setItems((prev) => {
        const seen = new Set(data.items.map((t) => t.id))
        const tail = prev.filter((t) => !seen.has(t.id))
        return [...data.items, ...tail]
      })
    }
  }, [data, multiMode])

  const loadMoreSingle = async () => {
    if (!cursor) return
    setLoadingMore(true)
    try {
      const res = await listTasks({ limit: PAGE_SIZE, cursor, status: statusFilter[0] })
      hasPagedRef.current = true
      setItems((prev) => [...prev, ...res.items])
      setCursor(res.next_cursor)
    } finally {
      setLoadingMore(false)
    }
  }

  const loadMoreMulti = async () => {
    const next: Record<string, string | undefined> = {}
    for (const [s, c] of Object.entries(cursors)) {
      if (c.hasMore && c.cursor) next[s] = c.cursor
    }
    if (Object.keys(next).length === 0) return
    setLoadingMore(true)
    try {
      const results = await Promise.all(
        statusFilter
          .filter((s) => next[s])
          .map(async (s) => {
            const res = await listTasks({ limit: PAGE_SIZE, status: s, cursor: next[s] })
            return [s, res] as const
          }),
      )
      const fresh = results.flatMap(([, r]) => r.items)
      setItems((prev) => {
        const seen = new Set(prev.map((t) => t.id))
        return [...prev, ...fresh.filter((t) => !seen.has(t.id))].sort(byCreatedDesc)
      })
      setCursors((prev) => {
        const out = { ...prev }
        for (const [s, r] of results) out[s] = { cursor: r.next_cursor, hasMore: r.has_more }
        return out
      })
      hasPagedRef.current = true
    } finally {
      setLoadingMore(false)
    }
  }

  const loadMore = () => (multiMode ? loadMoreMulti() : loadMoreSingle())
  const refresh = () => {
    if (multiMode) void fetchMultiPage({}, false)
    else void refetch()
  }

  const visibleItems = useMemo(
    () => (typeFilter ? items.filter((t) => t.type === typeFilter) : items),
    [items, typeFilter],
  )
  const hasMore = multiMode
    ? Object.values(cursors).some((c) => c.hasMore)
    : cursor != null
  const error = multiMode ? multiError : singleError

  const cancelTaskMutation = useCancelTask()
  const showApiError = useShowApiError()

  const handleCancel = (record: Task) => {
    Modal.confirm({
      title: '取消任务',
      content: `确认取消 ${typeMap[record.type] ?? record.type} 任务 ${record.id.slice(0, 12)}… 吗？`,
      okText: '确认取消',
      okButtonProps: { danger: true, icon: <StopOutlined /> },
      cancelText: '再等等',
      onOk: async () => {
        try {
          await cancelTaskMutation.mutateAsync(record.id)
          if (multiMode) void fetchMultiPage({}, false)
        } catch (e) {
          // 取消失败（任务已结束/后端拒绝）也要让用户看到原因
          showApiError(e, '取消任务失败')
        }
      },
    })
  }

  const columns = [
    {
      title: '任务 ID',
      dataIndex: 'id',
      ellipsis: true,
      width: 160,
      render: (v: string) => <EllipsisText value={v} code />,
    },
    {
      title: '类型',
      dataIndex: 'type',
      width: 80,
      render: (v: Task['type']) => typeMap[v] || v,
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 100,
      render: (v: Task['status']) => <Tag color={statusMap[v].color}>{statusMap[v].label}</Tag>,
    },
    {
      title: '批次',
      key: 'batch',
      width: 140,
      render: (_: unknown, record: Task) => {
        const batchId = record.related_id
        if (!batchId) return '-'
        return (
          <Button
            type="link"
            size="small"
            icon={<ApartmentOutlined />}
            style={{ padding: 0 }}
            onClick={() => setSelectedBatchId(batchId)}
          >
            {batchId.slice(0, 12)}…
          </Button>
        )
      },
    },
    {
      title: '进度',
      key: 'progress',
      render: (_: unknown, record: Task) => <ProgressCell task={record} />,
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      width: 150,
      render: (v: string) => formatDateTime(v),
    },
    {
      title: '结束时间',
      dataIndex: 'finished_at',
      width: 150,
      render: (v: string | null) => formatDateTime(v),
    },
    {
      title: '操作',
      key: 'action',
      width: 100,
      render: (_: unknown, record: Task) => {
        const cancellable = record.status === 'pending' || record.status === 'running' || record.status === 'cancelling'
        if (!cancellable) return '-'
        return (
          <Button
            size="small"
            danger
            icon={<StopOutlined />}
            loading={cancelTaskMutation.isPending}
            onClick={() => handleCancel(record)}
          >
            取消
          </Button>
        )
      },
    },
    {
      title: '结果',
      key: 'result',
      width: 140,
      render: (_: unknown, record: Task) => {
        if (record.status === 'done' && record.result?.snapshot_id) {
          return (
            <Button
              type="link"
              style={{ padding: 0 }}
              onClick={() => navigate(`/snapshots?snapshot=${encodeURIComponent(record.result!.snapshot_id!)}`)}
            >
              查看快照
            </Button>
          )
        }
        if (record.status === 'error' && record.error) {
          return <Text type="danger" ellipsis title={record.error}>失败</Text>
        }
        if (record.status === 'cancelled') {
          return <Text type="warning">已取消</Text>
        }
        return '-'
      },
    },
  ]

  return (
    <Card>
      <Space style={{ marginBottom: 12, justifyContent: 'space-between', width: '100%' }}>
        <Text strong style={{ fontSize: 16 }}>任务</Text>
        <Button icon={<ReloadOutlined />} loading={isLoading || multiLoading} onClick={refresh}>
          刷新
        </Button>
      </Space>

      <Space wrap style={{ marginBottom: 16 }}>
        <Select
          mode="multiple"
          allowClear
          placeholder="全部状态"
          style={{ minWidth: 280 }}
          value={statusFilter}
          onChange={applyStatusFilter}
          maxTagCount="responsive"
          options={FILTERABLE_STATUSES.map((s) => ({ value: s, label: statusMap[s].label }))}
        />
        <Tooltip title="类型筛选作用于已加载的任务（后端暂不支持按类型过滤）">
          <Select
            allowClear
            placeholder="全部类型"
            style={{ width: 150 }}
            value={typeFilter}
            onChange={(v) => setTypeFilter(v)}
            options={(Object.keys(typeMap) as Task['type'][]).map((t) => ({ value: t, label: typeMap[t] }))}
          />
        </Tooltip>
        {(statusFilter.length > 0 || typeFilter) && (
          <Text type="secondary" style={{ fontSize: 12 }}>
            {statusFilter.length > 1 ? '多状态筛选并行查询后合并' : statusFilter.length === 1 ? '按状态过滤' : null}
            {statusFilter.length > 0 && typeFilter ? '；' : ''}
            {typeFilter ? '类型为前端过滤' : ''}
          </Text>
        )}
      </Space>

      {error ? <ErrorAlert error={error} /> : null}

      {(isLoading || multiLoading) && !items.length ? (
        <Spin style={{ display: 'block', margin: '32px auto' }} />
      ) : visibleItems.length ? (
        <>
          <ResizableTable
            tableId="tasks"
            rowKey="id"
            size="small"
            tableLayout="fixed"
            columns={columns}
            dataSource={visibleItems}
            pagination={false}
            scroll={{ x: 'max-content' }}
          />
          {hasMore && !typeFilter && (
            <div style={{ textAlign: 'center', marginTop: 16 }}>
              <Button loading={loadingMore} onClick={loadMore}>
                加载更多
              </Button>
            </div>
          )}
        </>
      ) : (
        <Empty description={statusFilter.length || typeFilter ? '没有符合条件的任务' : '暂无任务'} />
      )}

      <BatchDrawer batchId={selectedBatchId} onClose={() => setSelectedBatchId(null)} />
    </Card>
  )
}

function BatchDrawer({ batchId, onClose }: { batchId: string | null; onClose: () => void }) {
  const { data: batch, isLoading } = useBatch(batchId ?? undefined, true)
  const { data: tasks } = useTasks({ batch_id: batchId ?? undefined, limit: 500 }, true)

  return (
    <Drawer
      title="批次详情"
      size={560}
      open={!!batchId}
      onClose={onClose}
    >
      {isLoading && !batch ? (
        <Spin style={{ display: 'block', margin: '32px auto' }} />
      ) : batch ? (
        <Space orientation="vertical" style={{ width: '100%' }} size="large">
          <div>
            <Text type="secondary">批次 ID</Text>
            <div><Text copyable>{batch.batch_id}</Text></div>
          </div>

          <div>
            <Text type="secondary">状态</Text>
            <div>
              <Tag color={batchStatusMap[batch.status].color}>
                {batchStatusMap[batch.status].label}
              </Tag>
            </div>
          </div>

          <div>
            <Text type="secondary">根路径</Text>
            <div><Text>{batch.root}</Text></div>
          </div>

          <div>
            <Text type="secondary">汇总</Text>
            <div>
              <Space>
                <Text>完成 {batch.summary?.done ?? 0}</Text>
                <Text type="danger">失败 {batch.summary?.error ?? 0}</Text>
                <Text type="warning">取消 {batch.summary?.cancelled ?? 0}</Text>
                <Text type="success">运行中 {batch.summary?.running ?? 0}</Text>
              </Space>
            </div>
          </div>

          <div>
            <Text type="secondary">计划卷</Text>
            <div>
              {batch.planned_volumes.map((vol, idx) => (
                <div key={idx}>
                  {vol.path} {vol.filesystem ? `(${vol.filesystem})` : ''}
                </div>
              ))}
            </div>
          </div>

          <div>
            <Text type="secondary">子任务</Text>
            <Space orientation="vertical" style={{ width: '100%' }} size="small">
              {tasks?.items.map((t) => (
                <div key={t.id} style={{ border: '1px solid #f0f0f0', borderRadius: 6, padding: 12 }}>
                  <Space style={{ justifyContent: 'space-between', width: '100%' }}>
                    <Text strong>{t.id.slice(0, 16)}…</Text>
                    <Tag color={statusMap[t.status].color}>{statusMap[t.status].label}</Tag>
                  </Space>
                  <div style={{ marginTop: 8 }}>
                    <ProgressCell task={t} />
                  </div>
                </div>
              ))}
              {!tasks?.items.length && <Text type="secondary">暂无子任务</Text>}
            </Space>
          </div>
        </Space>
      ) : (
        <Empty description="未找到批次" />
      )}
    </Drawer>
  )
}

function ProgressCell({ task }: { task: Task }) {
  if (task.status === 'done') {
    return <Progress percent={100} size="small" status="success" />
  }
  if (task.status === 'error' || task.status === 'cancelled' || task.status === 'cancelling') {
    return (
      <Space orientation="vertical" size={0} style={{ width: '100%' }}>
        <Progress percent={100} size="small" status="exception" showInfo={false} />
        <Text type="danger" style={{ fontSize: 12 }}>{task.error || formatTaskStatus(task.status)}</Text>
      </Space>
    )
  }
  if (task.progress == null) {
    return (
      <Space orientation="vertical" size={0} style={{ width: '100%' }}>
        <Progress percent={0} size="small" status="active" showInfo={false} />
        <Text type="secondary" style={{ fontSize: 12 }}>{formatTaskMessage(task.message)}</Text>
      </Space>
    )
  }
  return (
    <Space orientation="vertical" size={0} style={{ width: '100%' }}>
      <Progress percent={Math.round(task.progress * 100)} size="small" status="active" />
      <Text type="secondary" style={{ fontSize: 12 }}>{formatTaskMessage(task.message)}</Text>
    </Space>
  )
}
