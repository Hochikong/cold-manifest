import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Modal,
  Input,
  Button,
  Checkbox,
  Progress,
  Alert,
  Space,
  Typography,
  Tag,
  Form,
} from 'antd'
import {
  FolderOpenOutlined,
  PlayCircleOutlined,
  StopOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  CloudSyncOutlined,
} from '@ant-design/icons'
import { useQueryClient } from '@tanstack/react-query'
import { useCreateCollect, useCancelTask, useTaskEvents } from '../api/hooks'
import { formatTaskMessage, formatTaskStatus } from '../utils/taskMessage'
import { formatFileSize, formatNumber } from '../utils/format'
import type { Task } from '../api/client'

const { Text } = Typography
const { TextArea } = Input

const LAST_COLLECT_PATH_KEY = 'cldm-collect-last-path'
const DEFAULT_PATH = ''

type Mode = 'form' | 'progress' | 'done'

interface CollectDialogProps {
  open: boolean
  onClose: () => void
}

export default function CollectDialog({ open, onClose }: CollectDialogProps) {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const createCollectMutation = useCreateCollect()
  const cancelTaskMutation = useCancelTask()

  const [mode, setMode] = useState<Mode>('form')
  const [taskId, setTaskId] = useState<string | null>(null)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [cancelRequested, setCancelRequested] = useState(false)

  const [path, setPath] = useState(() => localStorage.getItem(LAST_COLLECT_PATH_KEY) || DEFAULT_PATH)
  const [serial, setSerial] = useState('')
  const [volumeId, setVolumeId] = useState('')
  const [excludeGlobsText, setExcludeGlobsText] = useState('')
  const [excludeHidden, setExcludeHidden] = useState(false)
  const [smartctl, setSmartctl] = useState(true)
  const [onDiskCopy, setOnDiskCopy] = useState(true)

  const { task } = useTaskEvents(taskId ?? undefined, mode === 'progress' || mode === 'done')
  const taskRef = useRef<Task | null>(null)
  taskRef.current = task

  useEffect(() => {
    if (!open) return
    // 每次打开时，如果之前的任务已经结束，就回到表单
    if (taskRef.current && isTerminal(taskRef.current.status)) {
      setMode('form')
      setTaskId(null)
      setSubmitError(null)
      setCancelRequested(false)
    }
  }, [open])

  useEffect(() => {
    if (!task) return
    if (mode === 'progress' && isTerminal(task.status)) {
      qc.invalidateQueries({ queryKey: ['snapshots'] })
      qc.invalidateQueries({ queryKey: ['volumes'] })
      qc.invalidateQueries({ queryKey: ['tasks'] })
      setMode('done')
    }
  }, [task, mode, qc])

  const excludeGlobs = useMemo(() => {
    return excludeGlobsText
      .split('\n')
      .map((s) => s.trim())
      .filter(Boolean)
  }, [excludeGlobsText])

  const startCollect = async () => {
    const trimmedPath = path.trim()
    if (!trimmedPath) return
    setSubmitError(null)
    localStorage.setItem(LAST_COLLECT_PATH_KEY, trimmedPath)

    try {
      const res = await createCollectMutation.mutateAsync({
        path: trimmedPath,
        volume_id: volumeId.trim() || null,
        serial: serial.trim() || null,
        exclude_globs: excludeGlobs,
        exclude_hidden: excludeHidden,
        include_system: true,
        smartctl,
        on_disk_copy: onDiskCopy,
      })
      setTaskId(res.task_id)
      setCancelRequested(false)
      setMode('progress')
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      setSubmitError(msg)
    }
  }

  const handleCancel = async () => {
    if (!taskId || cancelRequested) return
    try {
      await cancelTaskMutation.mutateAsync(taskId)
      // 取消请求已受理：本地禁用按钮，避免再点触发重复 409（终态前轮询有延迟）
      setCancelRequested(true)
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      setSubmitError(msg)
    }
  }

  const handleViewSnapshot = () => {
    const snapshotId = task?.result?.snapshot_id
    if (!snapshotId) return
    onClose()
    navigate(`/snapshots?snapshot=${encodeURIComponent(snapshotId)}`)
  }

  const canStart =
    !createCollectMutation.isPending && path.trim().length > 0

  const isActive = task && (task.status === 'pending' || task.status === 'running')

  return (
    <Modal
      title={
        <Space>
          <CloudSyncOutlined />
          <span>开始采集</span>
        </Space>
      }
      open={open}
      onCancel={onClose}
      width={640}
      footer={null}
    >
      <Space orientation="vertical" style={{ width: '100%' }} size="middle">
        {submitError && (mode === 'form' || mode === 'progress') && (
          <Alert type="error" showIcon title="提交失败" description={submitError} />
        )}

        {mode === 'form' && (
          <Form layout="vertical">
            <Form.Item label="采集路径" required>
              <Input
                placeholder="例如 /tmp/opencode/collect-demo"
                value={path}
                onChange={(e) => setPath(e.target.value)}
                onPressEnter={startCollect}
                prefix={<FolderOpenOutlined />}
              />
            </Form.Item>

            <Form.Item
              label="磁盘序列号"
              extra="USB 桥盘探测不到序列号时必须填写；普通 SATA 盘可留空"
            >
              <Input
                placeholder="例如 DEMO01"
                value={serial}
                onChange={(e) => setSerial(e.target.value)}
              />
            </Form.Item>

            <Form.Item label="卷 ID（可选）">
              <Input
                placeholder="默认由程序自动生成"
                value={volumeId}
                onChange={(e) => setVolumeId(e.target.value)}
              />
            </Form.Item>

            <Form.Item label="排除规则（每行一条 glob）">
              <TextArea
                rows={3}
                placeholder={".git\\nnode_modules\\n*.tmp"}
                value={excludeGlobsText}
                onChange={(e) => setExcludeGlobsText(e.target.value)}
              />
            </Form.Item>

            <Form.Item>
              <Space wrap>
                <Checkbox checked={excludeHidden} onChange={(e) => setExcludeHidden(e.target.checked)}>
                  排除隐藏文件
                </Checkbox>
                <Checkbox checked={smartctl} onChange={(e) => setSmartctl(e.target.checked)}>
                  读取 SMART 信息
                </Checkbox>
                <Checkbox checked={onDiskCopy} onChange={(e) => setOnDiskCopy(e.target.checked)}>
                  写入盘上副本
                </Checkbox>
              </Space>
            </Form.Item>

            <Button
              type="primary"
              icon={<PlayCircleOutlined />}
              loading={createCollectMutation.isPending}
              disabled={!canStart}
              onClick={startCollect}
              block
            >
              开始采集
            </Button>
          </Form>
        )}

        {(mode === 'progress' || mode === 'done') && task && (
          <>
            <Space style={{ justifyContent: 'space-between', width: '100%' }}>
              <Text strong>{task.payload.path as string}</Text>
              <StatusTag status={task.status} />
            </Space>

            <ProgressSection task={task} />

            {task.status === 'done' && (
              <Alert
                type="success"
                showIcon
                title="采集完成"
                description={<DoneDescription task={task} />}
              />
            )}
            {task.status === 'cancelled' && (
              <Alert type="warning" showIcon title="已取消" description={task.message ?? '采集任务已被取消'} />
            )}
            {task.status === 'error' && (
              <Alert type="error" showIcon title="采集失败" description={task.error ?? '未知错误'} />
            )}

            <Space wrap>
              {task.status === 'done' && task.result?.snapshot_id && (
                <Button type="primary" onClick={handleViewSnapshot}>
                  查看快照
                </Button>
              )}
              {isActive && (
                <Button
                  danger
                  icon={<StopOutlined />}
                  loading={cancelTaskMutation.isPending}
                  disabled={cancelRequested}
                  onClick={handleCancel}
                >
                  取消采集
                </Button>
              )}
              <Button onClick={onClose}>{isActive ? '后台运行' : '关闭'}</Button>
            </Space>
          </>
        )}
      </Space>
    </Modal>
  )
}

function StatusTag({ status }: { status: Task['status'] }) {
  if (status === 'done') {
    return <Tag color="success" icon={<CheckCircleOutlined />}>{formatTaskStatus(status)}</Tag>
  }
  if (status === 'error') {
    return <Tag color="error" icon={<CloseCircleOutlined />}>{formatTaskStatus(status)}</Tag>
  }
  if (status === 'cancelled') {
    return <Tag color="warning">{formatTaskStatus(status)}</Tag>
  }
  if (status === 'cancelling') {
    return <Tag color="processing">{formatTaskStatus(status)}</Tag>
  }
  if (status === 'running') {
    return <Tag color="processing">{formatTaskStatus(status)}</Tag>
  }
  return <Tag>{formatTaskStatus(status)}</Tag>
}

function ProgressSection({ task }: { task: Task }) {
  const message = formatTaskMessage(task.message)

  if (task.status === 'done') {
    return (
      <Space orientation="vertical" size={0} style={{ width: '100%' }}>
        <Progress percent={100} status="success" />
        <Text type="secondary">{message}</Text>
      </Space>
    )
  }

  if (task.status === 'error') {
    return (
      <Space orientation="vertical" size={0} style={{ width: '100%' }}>
        <Progress percent={100} status="exception" showInfo={false} />
        <Text type="danger">{task.error ?? '采集失败'}</Text>
      </Space>
    )
  }

  if (task.status === 'cancelled' || task.status === 'cancelling') {
    return (
      <Space orientation="vertical" size={0} style={{ width: '100%' }}>
        <Progress percent={100} status="exception" showInfo={false} />
        <Text type="secondary">{message}</Text>
      </Space>
    )
  }

  if (task.progress == null) {
    return (
      <Space orientation="vertical" size={0} style={{ width: '100%' }}>
        <Progress percent={0} status="active" showInfo={false} />
        <Text type="secondary">{message}</Text>
      </Space>
    )
  }

  return (
    <Space orientation="vertical" size={0} style={{ width: '100%' }}>
      <Progress percent={Math.round(task.progress * 100)} status="active" />
      <Text type="secondary">{message}</Text>
    </Space>
    )
}

function DoneDescription({ task }: { task: Task }) {
  const r = task.result
  if (!r) return '采集任务已完成'
  const parts: string[] = []
  if (typeof r.files === 'number') parts.push(`${formatNumber(r.files)} 个文件`)
  if (typeof r.dirs === 'number') parts.push(`${formatNumber(r.dirs)} 个目录`)
  if (typeof r.total_bytes === 'number') parts.push(formatFileSize(r.total_bytes))
  return parts.length ? parts.join(' · ') : '采集任务已完成'
}

function isTerminal(status: Task['status']): boolean {
  return status === 'done' || status === 'error' || status === 'cancelled'
}
