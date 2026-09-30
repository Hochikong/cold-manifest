import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Card, Table, Button, Tag, Progress, Space, Typography, Empty, Spin, Modal, Drawer } from 'antd'
import { ReloadOutlined, StopOutlined, ApartmentOutlined } from '@ant-design/icons'
import { useTasks, useCancelTask, useBatch } from '../api/hooks'
import { listTasks, type Task, type Batch } from '../api/client'
import ErrorAlert from '../components/ErrorAlert'
import { formatTaskMessage, formatTaskStatus } from '../utils/taskMessage'
import { formatDateTime } from '../utils/format'

const { Text } = Typography
const PAGE_SIZE = 20

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
}

const batchStatusMap: Record<Batch['status'], { label: string; color: string }> = {
  running: { label: '运行中', color: 'processing' },
  done: { label: '完成', color: 'success' },
  partial: { label: '部分完成', color: 'warning' },
}

export default function TasksPage() {
  const navigate = useNavigate()
  const [items, setItems] = useState<Task[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [loadingMore, setLoadingMore] = useState(false)
  const [selectedBatchId, setSelectedBatchId] = useState<string | null>(null)

  const { data, isLoading, error } = useTasks({ limit: PAGE_SIZE }, true)
  const cancelTaskMutation = useCancelTask()
  const hasPagedRef = useRef(false)

  useEffect(() => {
    if (data) {
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
    }
  }, [data])

  const loadMore = async () => {
    if (!cursor) return
    setLoadingMore(true)
    try {
      const res = await listTasks({ limit: PAGE_SIZE, cursor })
      hasPagedRef.current = true
      setItems((prev) => [...prev, ...res.items])
      setCursor(res.next_cursor)
    } finally {
      setLoadingMore(false)
    }
  }

  const handleCancel = (record: Task) => {
    Modal.confirm({
      title: '取消任务',
      content: `确认取消 ${typeMap[record.type] ?? record.type} 任务 ${record.id.slice(0, 12)}… 吗？`,
      okText: '确认取消',
      okButtonProps: { danger: true, icon: <StopOutlined /> },
      cancelText: '再等等',
      onOk: async () => {
        await cancelTaskMutation.mutateAsync(record.id)
      },
    })
  }

  const columns = [
    {
      title: '任务 ID',
      dataIndex: 'id',
      ellipsis: true,
      width: 160,
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
      <Space style={{ marginBottom: 16, justifyContent: 'space-between', width: '100%' }}>
        <Text strong style={{ fontSize: 16 }}>任务</Text>
        <Button icon={<ReloadOutlined />} loading={isLoading} onClick={() => window.location.reload()}>
          刷新
        </Button>
      </Space>

      {error && <ErrorAlert error={error} />}

      {isLoading && !items.length ? (
        <Spin style={{ display: 'block', margin: '32px auto' }} />
      ) : items.length ? (
        <>
          <Table
            rowKey="id"
            size="small"
            columns={columns}
            dataSource={items}
            pagination={false}
            scroll={{ x: 'max-content' }}
          />
          {cursor && (
            <div style={{ textAlign: 'center', marginTop: 16 }}>
              <Button loading={loadingMore} onClick={loadMore}>
                加载更多
              </Button>
            </div>
          )}
        </>
      ) : (
        <Empty description="暂无任务" />
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
