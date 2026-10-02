import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Card, Button, Tag, Progress, Space, Typography, Empty, Spin, Modal, Drawer, Select, Tooltip, App } from 'antd'
import { ReloadOutlined, StopOutlined, ApartmentOutlined, DeleteOutlined } from '@ant-design/icons'
import { useTasks, useCancelTask, useDeleteTask, useBatch, useSnapshots } from '../api/hooks'
import { listTasks, type Task, type Batch, type TaskStatus } from '../api/client'
import ErrorAlert from '../components/ErrorAlert'
import EllipsisText from '../components/EllipsisText'
import CursorPager from '../components/CursorPager'
import { useShowApiError } from '../utils/apiError'
import { formatTaskMessage, formatTaskStatus } from '../utils/taskMessage'
import { formatDateTime } from '../utils/format'
import ResizableTable from '../components/ResizableTable'
import { useCursorPaging } from '../hooks/useCursorPaging'

const { Text } = Typography

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
  const { message } = App.useApp()
  const [selectedBatchId, setSelectedBatchId] = useState<string | null>(null)

  const [statusFilter, setStatusFilter] = useState<TaskStatus[]>([])
  const [typeFilter, setTypeFilter] = useState<Task['type'] | undefined>(undefined)
  const multiMode = statusFilter.length >= 2

  // cursor 栈分页：单状态栈存 cursor（null=第一页），多状态栈存各状态的 cursor map
  // 共享 hook：行数按 cldm_pagesize:tasks 持久化、当前页写进 URL ?page=（仅单状态模式）
  const {
    pageSize,
    changePageSize: changePageSizeBase,
    cursorStack,
    pageIndex,
    goNext: goNextSingle,
    goPrev: goPrevSingle,
    resetPage,
    restoring,
  } = useCursorPaging({
    tableId: 'tasks',
    fetchPage: (limit, cur) => listTasks({ limit, status: statusFilter[0], cursor: cur }),
  })
  const [multiMaps, setMultiMaps] = useState<Record<string, string | undefined>[]>([{}])
  const multiIndex = multiMaps.length - 1
  const tableTopRef = useRef<HTMLDivElement | null>(null)
  const mountedRef = useRef(false)

  // 单请求模式（未筛选 / 单状态）：走 useTasks，活跃任务 4s 轮询
  const { data, isLoading, error: singleError, refetch } = useTasks(
    { limit: pageSize, status: statusFilter[0], cursor: cursorStack[pageIndex] ?? undefined },
    !multiMode,
  )

  // 多状态并行合并：当前页 items 与各状态的 cursor/hasMore
  const [multiItems, setMultiItems] = useState<Task[]>([])
  const [multiLoading, setMultiLoading] = useState(false)
  const [multiError, setMultiError] = useState<unknown>(null)
  const [multiCursors, setMultiCursors] = useState<Record<string, StatusCursor>>({})
  const hasActiveRef = useRef(false)

  useEffect(() => {
    hasActiveRef.current = multiItems.some(isActiveStatus)
  }, [multiItems])

  /** 筛选变化 → 重置回第一页（渲染期同步，避免 effect 级联）。 */
  const filterKey = `${statusFilter.join(',')}|${pageSize}`
  const [syncedFilterKey, setSyncedFilterKey] = useState(filterKey)
  if (filterKey !== syncedFilterKey) {
    setSyncedFilterKey(filterKey)
    resetPage()
    setMultiMaps([{}])
    setMultiItems([])
    setMultiCursors({})
    setMultiError(null)
  }

  /** 拉一页（每状态各一页）并按创建时间倒序合并去重；轮询复用同一 map 刷新当前页。 */
  const fetchMultiPage = useCallback(
    async (map: Record<string, string | undefined>) => {
      setMultiLoading(true)
      try {
        const results = await Promise.all(
          statusFilter.map(async (s) => {
            const res = await listTasks({ limit: pageSize, status: s, cursor: map[s] })
            return [s, res] as const
          }),
        )
        const fresh = results.flatMap(([, r]) => r.items)
        // 各状态各一页，fresh 内部无重复；直接整页替换（轮询/翻页都不残留旧行）
        const seen = new Set<number | string>()
        const merged = fresh.filter((t) => (seen.has(t.id) ? false : (seen.add(t.id), true)))
        setMultiItems(merged.sort(byCreatedDesc))
        setMultiCursors(
          Object.fromEntries(results.map(([s, r]) => [s, { cursor: r.next_cursor, hasMore: r.has_more }])),
        )
        setMultiError(null)
      } catch (e) {
        setMultiError(e)
      } finally {
        setMultiLoading(false)
      }
    },
    [statusFilter, pageSize],
  )

  // 多状态模式：map/页码变化时拉取；活跃任务 4s 轮询刷新当前页
  useEffect(() => {
    if (!multiMode) return
    const first = window.setTimeout(() => {
      void fetchMultiPage(multiMaps[multiIndex])
    }, 0)
    const timer = window.setInterval(() => {
      if (hasActiveRef.current) void fetchMultiPage(multiMaps[multiIndex])
    }, 4_000)
    return () => {
      window.clearTimeout(first)
      window.clearInterval(timer)
    }
  }, [multiMode, fetchMultiPage, multiMaps, multiIndex])

  // 翻页 / 改行数后滚回表格顶部（首次挂载不滚）
  useEffect(() => {
    if (!mountedRef.current) {
      mountedRef.current = true
      return
    }
    tableTopRef.current?.scrollIntoView({ block: 'start', behavior: 'smooth' })
  }, [pageIndex, multiIndex, pageSize])

  const goNext = () => {
    if (multiMode) {
      const next: Record<string, string | undefined> = {}
      let any = false
      for (const [s, c] of Object.entries(multiCursors)) {
        if (c.hasMore && c.cursor) {
          next[s] = c.cursor
          any = true
        }
      }
      if (any) setMultiMaps((maps) => [...maps, next])
    } else if (data?.has_more && data.next_cursor) {
      goNextSingle(data.next_cursor)
    }
  }
  const goPrev = () => {
    if (multiMode) setMultiMaps((maps) => (maps.length > 1 ? maps.slice(0, -1) : maps))
    else goPrevSingle()
  }
  const changePageSize = (n: number) => {
    changePageSizeBase(n)
    setMultiMaps([{}])
    setMultiItems([])
    setMultiCursors({})
  }

  const refresh = () => {
    if (multiMode) void fetchMultiPage(multiMaps[multiIndex])
    else void refetch()
  }

  const items = useMemo(
    () => (multiMode ? multiItems : (data?.items ?? [])),
    [multiMode, multiItems, data],
  )
  const canNext = multiMode
    ? Object.values(multiCursors).some((c) => c.hasMore)
    : (data?.has_more ?? false)
  const error = multiMode ? multiError : singleError

  const visibleItems = useMemo(
    () => (typeFilter ? items.filter((t) => t.type === typeFilter) : items),
    [items, typeFilter],
  )

  const cancelTaskMutation = useCancelTask()
  const deleteTaskMutation = useDeleteTask()
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
          if (multiMode) void fetchMultiPage(multiMaps[multiIndex])
        } catch (e) {
          // 取消失败（任务已结束/后端拒绝）也要让用户看到原因
          showApiError(e, '取消任务失败')
        }
      },
    })
  }

  const handleDelete = (record: Task) => {
    Modal.confirm({
      title: '删除任务记录',
      content: `确认删除 ${typeMap[record.type] ?? record.type} 任务 ${record.id.slice(0, 12)}… 的记录吗？只删除这条任务记录，不影响已采集的快照与对比数据。`,
      okText: '确认删除',
      okButtonProps: { danger: true, icon: <DeleteOutlined /> },
      cancelText: '取消',
      onOk: async () => {
        try {
          await deleteTaskMutation.mutateAsync(record.id)
          message.success('任务记录已删除')
          // 删掉当前页最后一条且不是第一页 → 回退一页再刷新，避免落在空页上
          const wasLastRowOnPage = visibleItems.length === 1 && visibleItems[0].id === record.id
          if (wasLastRowOnPage && (multiMode ? multiIndex > 0 : pageIndex > 0)) goPrev()
          if (multiMode) void fetchMultiPage(multiMaps[Math.max(0, multiMaps.length - (wasLastRowOnPage && multiIndex > 0 ? 2 : 1))])
          else void refetch()
        } catch (e) {
          // 例如刚被别的操作改成运行中 → 409，给出后端可读原因而非裸错误
          showApiError(e, '删除任务失败')
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
      width: 150,
      render: (_: unknown, record: Task) => {
        const cancellable = record.status === 'pending' || record.status === 'running' || record.status === 'cancelling'
        const deleteDisabled = cancellable
        return (
          <Space size={4}>
            {cancellable && (
              <Button
                size="small"
                danger
                icon={<StopOutlined />}
                loading={cancelTaskMutation.isPending}
                onClick={() => handleCancel(record)}
              >
                取消
              </Button>
            )}
            <Tooltip title={deleteDisabled ? '任务正在运行或排队中，请先取消或等它结束' : '删除这条任务记录'}>
              <Button
                size="small"
                danger
                type="text"
                icon={<DeleteOutlined />}
                aria-label="删除任务记录"
                disabled={deleteDisabled}
                loading={deleteTaskMutation.isPending && deleteTaskMutation.variables === record.id}
                onClick={() => handleDelete(record)}
              />
            </Tooltip>
          </Space>
        )
      },
    },
    {
      title: '结果',
      key: 'result',
      width: 180,
      render: (_: unknown, record: Task) => {
        if (record.status === 'done' && record.result?.snapshot_id) {
          return <SnapshotJumpCell snapshotId={record.result.snapshot_id} />
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
          onChange={(v) => setStatusFilter(v)}
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
          <div ref={tableTopRef} />
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
          {!typeFilter && (
            <CursorPager
              pageSize={pageSize}
              onPageSizeChange={changePageSize}
              canPrev={multiMode ? multiIndex > 0 : pageIndex > 0}
              onPrev={goPrev}
              canNext={canNext}
              onNext={goNext}
              disabled={restoring}
              hint={restoring ? '正在恢复页码…' : `第 ${(multiMode ? multiIndex : pageIndex) + 1} 页 · 本页 ${visibleItems.length} 条`}
            />
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

/** 结果列的"查看快照"：快照已被删除时显示标记并禁用跳转。 */
function SnapshotJumpCell({ snapshotId }: { snapshotId: string }) {
  const navigate = useNavigate()
  // 用快照列表做存在性判断（快照总数 ≤20，单次 200 请求缓存共享），
  // 不逐个 GET 详情——已删快照的 404 会污染 console。
  const { data: snapshots } = useSnapshots()
  const missing = !!snapshots && !snapshots.items.some((s) => s.snapshot_id === snapshotId)
  if (missing) {
    return (
      <Space size={4} wrap>
        <Tooltip title="该快照已删除或不存在">
          <Tag style={{ margin: 0 }}>快照已删除</Tag>
        </Tooltip>
        <Tooltip title="该快照已删除或不存在，无法跳转">
          <Button type="link" size="small" disabled style={{ padding: 0 }}>
            查看快照
          </Button>
        </Tooltip>
      </Space>
    )
  }
  return (
    <Button
      type="link"
      style={{ padding: 0 }}
      onClick={() => navigate(`/snapshots?snapshot=${encodeURIComponent(snapshotId)}`)}
    >
      查看快照
    </Button>
  )
}

function ProgressCell({ task }: { task: Task }) {  if (task.status === 'done') {
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
