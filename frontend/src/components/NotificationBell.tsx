import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { BellOutlined } from '@ant-design/icons'
import { Badge, Button, Empty, Popover, Space, Tag, Typography } from 'antd'
import type { Task } from '../api/client'
import { formatRelativeTime } from '../utils/format'
import { getTaskSummary, statusColor, statusLabel, taskTypeLabel } from '../utils/taskNotification'
import type { TaskNotification } from '../hooks/useTaskNotifications'

interface NotificationBellProps {
  notifications: TaskNotification[]
  unreadCount: number
  onMarkAllRead: () => void
}

export default function NotificationBell({
  notifications,
  unreadCount,
  onMarkAllRead,
}: NotificationBellProps) {
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)

  const handleClick = (task: Task) => {
    const result = task.result ?? {}
    const snapshotId = typeof result.snapshot_id === 'string' ? result.snapshot_id : undefined
    const diffId = typeof result.diff_id === 'string' ? result.diff_id : undefined
    if (snapshotId) {
      navigate(`/snapshots?snapshot=${encodeURIComponent(snapshotId)}`)
    } else if (diffId) {
      navigate(`/diff?id=${encodeURIComponent(diffId)}`)
    } else {
      navigate('/tasks')
    }
    setOpen(false)
  }

  const content = (
    <div style={{ width: 360, maxHeight: 420, overflow: 'auto' }}>
      <style>{`
        .notification-item {
          cursor: pointer;
          padding: 8px 12px;
          display: flex;
          align-items: flex-start;
          gap: 8px;
          transition: background 0.2s;
        }
        .notification-item:hover {
          background: rgba(0, 0, 0, 0.04) !important;
        }
      `}</style>
      <Space style={{ justifyContent: 'space-between', width: '100%', marginBottom: 8 }}>
        <Typography.Text strong>通知中心</Typography.Text>
        <Button
          type="link"
          size="small"
          onClick={onMarkAllRead}
          disabled={unreadCount === 0}
        >
          全部标记已读
        </Button>
      </Space>
      {notifications.length > 0 ? (
        <div>
          {notifications.map(({ task, isUnread }) => (
            <div
              key={task.id}
              className="notification-item"
              style={{
                background: isUnread ? 'var(--accent-bg, rgba(170, 59, 255, 0.06))' : undefined,
              }}
              onClick={() => handleClick(task)}
            >
              <div style={{ flex: 1, minWidth: 0 }}>
                <Space size={4} wrap>
                  {isUnread && (
                    <span
                      style={{
                        width: 6,
                        height: 6,
                        borderRadius: '50%',
                        background: 'var(--accent, #aa3bff)',
                        display: 'inline-block',
                        flexShrink: 0,
                        marginTop: 6,
                      }}
                    />
                  )}
                  <Tag color={statusColor(task.status)}>{statusLabel(task.status)}</Tag>
                  <Typography.Text strong>{taskTypeLabel(task.type)}</Typography.Text>
                </Space>
                <div>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    {getTaskSummary(task)}
                  </Typography.Text>
                </div>
              </div>
              <Typography.Text
                type="secondary"
                style={{ fontSize: 12, whiteSpace: 'nowrap', marginTop: 2 }}
              >
                {formatRelativeTime(task.finished_at ?? task.created_at)}
              </Typography.Text>
            </div>
          ))}
        </div>
      ) : (
        <Empty
          description="暂无已完成任务"
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          style={{ margin: '16px 0' }}
        />
      )}
    </div>
  )

  return (
    <Popover
      placement="bottomRight"
      trigger="click"
      open={open}
      onOpenChange={setOpen}
      content={content}
    >
      <Badge count={unreadCount} size="small" offset={[-4, 4]}>
        <Button type="text" icon={<BellOutlined />} aria-label="通知中心" />
      </Badge>
    </Popover>
  )
}
