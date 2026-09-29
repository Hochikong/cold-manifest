import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Card, Table, Button, Tag, Progress, Space, Typography, Empty, Spin } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { useTasks } from '../api/hooks'
import { listTasks, type Task } from '../api/client'
import ErrorAlert from '../components/ErrorAlert'
import { formatDateTime } from '../utils/format'

const { Text } = Typography
const PAGE_SIZE = 20

const statusMap: Record<Task['status'], { label: string; color: string }> = {
  pending: { label: '待处理', color: 'default' },
  running: { label: '运行中', color: 'processing' },
  done: { label: '完成', color: 'success' },
  error: { label: '失败', color: 'error' },
}

const typeMap: Record<Task['type'], string> = {
  import: '导入',
}

export default function TasksPage() {
  const navigate = useNavigate()
  const [items, setItems] = useState<Task[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [loadingMore, setLoadingMore] = useState(false)

  const { data, isLoading, error } = useTasks({ limit: PAGE_SIZE }, true)

  useEffect(() => {
    if (data) {
      setItems(data.items)
      setCursor(data.next_cursor)
    }
  }, [data])

  const loadMore = async () => {
    if (!cursor) return
    setLoadingMore(true)
    try {
      const res = await listTasks({ limit: PAGE_SIZE, cursor })
      setItems((prev) => [...prev, ...res.items])
      setCursor(res.next_cursor)
    } finally {
      setLoadingMore(false)
    }
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
    </Card>
  )
}

function ProgressCell({ task }: { task: Task }) {
  if (task.status === 'done') {
    return <Progress percent={100} size="small" status="success" />
  }
  if (task.status === 'error') {
    return (
      <Space direction="vertical" size={0} style={{ width: '100%' }}>
        <Progress percent={100} size="small" status="exception" showInfo={false} />
        <Text type="danger" style={{ fontSize: 12 }}>{task.error || '任务失败'}</Text>
      </Space>
    )
  }
  if (task.progress == null) {
    return (
      <Space direction="vertical" size={0} style={{ width: '100%' }}>
        <Progress percent={0} size="small" status="active" showInfo={false} />
        <Text type="secondary" style={{ fontSize: 12 }}>{formatMessage(task.message)}</Text>
      </Space>
    )
  }
  return (
    <Space direction="vertical" size={0} style={{ width: '100%' }}>
      <Progress percent={Math.round(task.progress * 100)} size="small" status="active" />
      <Text type="secondary" style={{ fontSize: 12 }}>{formatMessage(task.message)}</Text>
    </Space>
  )
}

function formatMessage(message: string | null): string {
  if (!message) return '准备中'
  if (message.startsWith('parse:')) {
    const parts = message.split(':')[1]
    if (parts) {
      const [done] = parts.split('/')
      return `已解析 ${Number(done).toLocaleString('zh-CN')} 行`
    }
  }
  const m = message.match(/^(\w+):(\d+)\/(\d+)$/)
  if (m) {
    const [, phase, done, total] = m
    const phaseName = phase === 'seal' ? '封存' : phase === 'done' ? '完成' : phase
    return `${phaseName} ${done}/${total}`
  }
  return message
}
