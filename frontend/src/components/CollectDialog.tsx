import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Modal,
  Input,
  Button,
  Checkbox,
  Progress,
  Alert,
  Select,
  Space,
  Typography,
  Tag,
  Form,
  Collapse,
  Divider,
  App,
} from 'antd'
import {
  FolderOpenOutlined,
  PlayCircleOutlined,
  StopOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  CloudSyncOutlined,
  PartitionOutlined,
  ReloadOutlined,
  SafetyOutlined,
  WarningOutlined,
} from '@ant-design/icons'
import { useQueryClient } from '@tanstack/react-query'
import { useAttachedDisks, useCreateCollect, useCancelTask, useTaskEvents, useBatch, useCollectPreflight, useSnapshot, useDisk } from '../api/hooks'
import { apiErrorDetail, getSnapshot, isBatchCollectResponse, patchDiskNickname, patchVolumeNickname, type BatchCollectCreateResponse, type Task, type Batch, type AttachedDisk } from '../api/client'
import { formatTaskMessage, formatTaskStatus } from '../utils/taskMessage'
import { formatFileSize, formatNumber } from '../utils/format'

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
  const { message } = App.useApp()
  const createCollectMutation = useCreateCollect()
  const cancelTaskMutation = useCancelTask()

  const [mode, setMode] = useState<Mode>('form')
  const [taskId, setTaskId] = useState<string | null>(null)
  const [batchResponse, setBatchResponse] = useState<BatchCollectCreateResponse | null>(null)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [cancelRequested, setCancelRequested] = useState(false)

  const [path, setPath] = useState(() => localStorage.getItem(LAST_COLLECT_PATH_KEY) || DEFAULT_PATH)
  const [serial, setSerial] = useState('')
  const [volumeId, setVolumeId] = useState('')
  const [nickname, setNickname] = useState('')
  const [excludeGlobsText, setExcludeGlobsText] = useState('')
  const [excludeHidden, setExcludeHidden] = useState(false)
  const [smartctl, setSmartctl] = useState(true)
  const [onDiskCopy, setOnDiskCopy] = useState(true)
  const [allPartitions, setAllPartitions] = useState(false)
  const [resume, setResume] = useState(false)
  const [crossFilesystems, setCrossFilesystems] = useState(false)

  const { task } = useTaskEvents(taskId ?? undefined, mode === 'progress' || mode === 'done')
  const { data: batch } = useBatch(batchResponse?.batch_id ?? undefined, mode === 'progress' || mode === 'done')

  useEffect(() => {
    if (!open) return
    // 每次打开对话框时回到干净表单；不能依赖 task/batchResponse（否则批次刚提交就被重置回表单）
    setMode('form')
    setTaskId(null)
    setBatchResponse(null)
    setSubmitError(null)
    setCancelRequested(false)
    setNickname('')
  }, [open])

  /**
   * 采集成功后把填写的昵称打到本次的盘（以及卷）上。
   * 失败不阻塞主流程：只给一条温和提示，稍后可在磁盘页手动补设。
   */
  const applyNickname = useCallback(
    async (snapshotIds: string[]) => {
      const nick = nickname.trim()
      if (!nick || snapshotIds.length === 0) return
      try {
        const diskIds = new Set<string>()
        const volumeIds = new Set<string>()
        for (const sid of snapshotIds) {
          try {
            const snap = await getSnapshot(sid)
            if (snap.disk_id) diskIds.add(snap.disk_id)
            if (snap.volume_id) volumeIds.add(snap.volume_id)
          } catch {
            // 单个快照详情读不到就跳过该卷，不影响其余
          }
        }
        await Promise.all([
          ...[...diskIds].map((d) => patchDiskNickname(d, nick)),
          ...[...volumeIds].map((v) => patchVolumeNickname(v, nick)),
        ])
        qc.invalidateQueries({ queryKey: ['disks'] })
        qc.invalidateQueries({ queryKey: ['disk'] })
        qc.invalidateQueries({ queryKey: ['volumes'] })
        qc.invalidateQueries({ queryKey: ['volume-detail'] })
        qc.invalidateQueries({ queryKey: ['snapshots'] })
        qc.invalidateQueries({ queryKey: ['snapshot'] })
        qc.invalidateQueries({ queryKey: ['diffs'] })
        message.success(`昵称「${nick}」已设置`)
      } catch {
        message.warning('昵称设置失败，不影响本次采集；可稍后在磁盘页手动设置')
      }
    },
    [nickname, qc, message],
  )

  useEffect(() => {
    if (!task) return
    if (mode === 'progress' && isTerminal(task.status)) {
      qc.invalidateQueries({ queryKey: ['snapshots'] })
      qc.invalidateQueries({ queryKey: ['volumes'] })
      qc.invalidateQueries({ queryKey: ['disks'] })
      qc.invalidateQueries({ queryKey: ['snapshot'] })
      qc.invalidateQueries({ queryKey: ['volume-trends'] })
      qc.invalidateQueries({ queryKey: ['tasks'] })
      if (task.status === 'done') {
        void applyNickname(task.result?.snapshot_id ? [task.result.snapshot_id] : [])
      }
      setMode('done')
    }
  }, [task, mode, qc, applyNickname])

  useEffect(() => {
    if (!batch) return
    if (mode === 'progress' && batch.status !== 'running') {
      qc.invalidateQueries({ queryKey: ['snapshots'] })
      qc.invalidateQueries({ queryKey: ['volumes'] })
      qc.invalidateQueries({ queryKey: ['disks'] })
      qc.invalidateQueries({ queryKey: ['snapshot'] })
      qc.invalidateQueries({ queryKey: ['volume-trends'] })
      qc.invalidateQueries({ queryKey: ['tasks'] })
      // 批次里所有成功的卷共享同一块盘：盘只打一次昵称，每个卷各打一次
      const doneSnapshotIds = (batch.tasks ?? [])
        .filter((t) => t.status === 'done' && t.result?.snapshot_id)
        .map((t) => t.result!.snapshot_id!)
      if (doneSnapshotIds.length > 0) {
        void applyNickname(doneSnapshotIds)
      }
      setMode('done')
    }
  }, [batch, mode, qc, applyNickname])

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
        all_partitions: allPartitions,
        resume,
        cross_filesystems: crossFilesystems,
      })

      if (isBatchCollectResponse(res)) {
        setBatchResponse(res)
        setCancelRequested(false)
        setMode('progress')
      } else {
        setTaskId(res.task_id)
        setCancelRequested(false)
        setMode('progress')
      }
    } catch (e) {
      // 后端 400/409 的中文 detail（如「数据根被占用」）优先展示
      setSubmitError(apiErrorDetail(e))
    }
  }

  const handleCancel = async () => {
    if (cancelRequested) return
    const ids = batchResponse?.task_ids ?? (taskId ? [taskId] : [])
    if (!ids.length) return
    try {
      await Promise.all(ids.map((id) => cancelTaskMutation.mutateAsync(id)))
      setCancelRequested(true)
    } catch (e) {
      setSubmitError(apiErrorDetail(e))
    }
  }

  const handleViewSnapshot = (snapshotId: string) => {
    if (!snapshotId) return
    onClose()
    navigate(`/snapshots?snapshot=${encodeURIComponent(snapshotId)}`)
  }

  const { data: preflight } = useCollectPreflight(path)

  const canStart =
    !createCollectMutation.isPending && path.trim().length > 0 && preflight?.writable !== false

  const singleTask = taskId ? task : null
  const isSingleActive = singleTask && (singleTask.status === 'pending' || singleTask.status === 'running')

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
      width={720}
      footer={null}
    >
      <Space orientation="vertical" style={{ width: '100%' }} size="middle">
        {submitError && (mode === 'form' || mode === 'progress') && (
          <Alert type="error" showIcon title="提交失败" description={submitError} />
        )}

        {mode === 'form' && (
          <Form layout="vertical">
            <AttachedDiskQuickPick
              onPick={(pickedPath, pickedSerial) => {
                setPath(pickedPath)
                if (pickedSerial && !serial.trim()) setSerial(pickedSerial)
              }}
            />

            <Form.Item label={allPartitions ? '盘 / 挂载点路径' : '采集路径'} required>
              <Input
                placeholder={allPartitions ? '例如 /tmp/opencode/collect-demo' : '例如 /tmp/opencode/collect-demo'}
                value={path}
                onChange={(e) => setPath(e.target.value)}
                onPressEnter={startCollect}
                prefix={<FolderOpenOutlined />}
              />
            </Form.Item>

            <PreflightSection path={path} />

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

            {!allPartitions && (
              <Form.Item label="卷 ID（可选）">
                <Input
                  placeholder="默认由程序自动生成"
                  value={volumeId}
                  onChange={(e) => setVolumeId(e.target.value)}
                />
              </Form.Item>
            )}

            <Form.Item label="昵称（可选）" extra="设置后可在磁盘/卷/对比页辨认">
              <Input
                placeholder="例如：视频盘 A"
                allowClear
                maxLength={64}
                value={nickname}
                onChange={(e) => setNickname(e.target.value)}
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

            <Form.Item>
              <Space orientation="vertical" size={0}>
                <Checkbox checked={allPartitions} onChange={(e) => setAllPartitions(e.target.checked)}>
                  <Space>
                    <PartitionOutlined />
                    <span>采集整盘所有分区</span>
                  </Space>
                </Checkbox>
                <Text type="secondary" style={{ fontSize: 12, marginLeft: 24 }}>
                  勾选后，上方路径视为盘或挂载点，程序会枚举该盘所有卷并建立批次
                </Text>
              </Space>
            </Form.Item>

            <Form.Item>
              <Space orientation="vertical" size={0}>
                <Checkbox checked={resume} onChange={(e) => setResume(e.target.checked)}>
                  <Space>
                    <ReloadOutlined />
                    <span>续采上次中断</span>
                  </Space>
                </Checkbox>
                <Text type="secondary" style={{ fontSize: 12, marginLeft: 24 }}>
                  优先续采该卷最新未封库（含 journal）的目录；单卷模式生效
                </Text>
              </Space>
            </Form.Item>

            <Collapse ghost items={[
              {
                key: 'advanced',
                label: '高级选项',
                children: (
                  <Checkbox checked={crossFilesystems} onChange={(e) => setCrossFilesystems(e.target.checked)}>
                    跨文件系统扫描（默认只采集同一文件系统）
                  </Checkbox>
                ),
              },
            ]} />

            <Button
              type="primary"
              icon={<PlayCircleOutlined />}
              loading={createCollectMutation.isPending}
              disabled={!canStart}
              onClick={startCollect}
              block
              style={{ marginTop: 12 }}
            >
              {allPartitions ? '开始批次采集' : '开始采集'}
            </Button>
          </Form>
        )}

        {(mode === 'progress' || mode === 'done') && batchResponse && (
          <BatchPanel
            batchResponse={batchResponse}
            batch={batch}
            mode={mode}
            cancelRequested={cancelRequested}
            onCancel={handleCancel}
            onViewSnapshot={handleViewSnapshot}
            onClose={onClose}
          />
        )}

        {(mode === 'progress' || mode === 'done') && !batchResponse && singleTask && (
          <>
            <Space style={{ justifyContent: 'space-between', width: '100%' }}>
              <Text strong>{singleTask.payload.path as string}</Text>
              <StatusTag status={singleTask.status} />
            </Space>

            <ProgressSection task={singleTask} />

            {singleTask.status === 'done' && (
              <Alert
                type="success"
                showIcon
                title="采集完成"
                description={<DoneDescription task={singleTask} />}
              />
            )}
            {singleTask.status === 'done' && singleTask.result?.snapshot_id && (
              <CollectSmartHint snapshotId={singleTask.result.snapshot_id} />
            )}
            {singleTask.status === 'cancelled' && (
              <Alert type="warning" showIcon title="已取消" description={singleTask.message ?? '采集任务已被取消'} />
            )}
            {singleTask.status === 'error' && (
              <Alert type="error" showIcon title="采集失败" description={singleTask.error ?? '未知错误'} />
            )}

            <Space wrap>
              {singleTask.status === 'done' && singleTask.result?.snapshot_id && (
                <Button type="primary" onClick={() => handleViewSnapshot(singleTask.result!.snapshot_id!)}>
                  查看快照
                </Button>
              )}
              {isSingleActive && (
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
              <Button onClick={onClose}>{isSingleActive ? '后台运行' : '关闭'}</Button>
            </Space>
          </>
        )}
      </Space>
    </Modal>
  )
}

interface BatchPanelProps {
  batchResponse: BatchCollectCreateResponse
  batch: Batch | undefined
  mode: Mode
  cancelRequested: boolean
  onCancel: () => void
  onViewSnapshot: (snapshotId: string) => void
  onClose: () => void
}

function BatchPanel({ batchResponse, batch, mode, cancelRequested, onCancel, onViewSnapshot, onClose }: BatchPanelProps) {
  const { batch_id, planned_volumes, task_ids, warnings } = batchResponse
  const batchStatus = batch?.status ?? 'running'
  const summary = batch?.summary ?? {}
  const doneCount = (summary.done ?? 0) + (summary.error ?? 0) + (summary.cancelled ?? 0)
  const errorCount = summary.error ?? 0
  const isActive = mode === 'progress' && batchStatus === 'running'

  return (
    <Space orientation="vertical" style={{ width: '100%' }} size="middle">
      <Space style={{ justifyContent: 'space-between', width: '100%' }}>
        <Text strong>
          <PartitionOutlined /> 批次 {batch_id.slice(0, 16)}…
        </Text>
        <BatchStatusTag status={batchStatus} />
      </Space>

      <Space size="large">
        <Text type="secondary">计划卷：{planned_volumes.length} 个</Text>
        <Text type="secondary">已完成：{doneCount}/{task_ids.length}</Text>
        {errorCount > 0 && <Text type="danger">失败 {errorCount} 个</Text>}
      </Space>

      {warnings.length > 0 && (
        <Alert type="warning" showIcon title="批次警告" description={warnings.join('；')} />
      )}

      <Collapse
        defaultActiveKey={['volumes']}
        items={[
          {
            key: 'volumes',
            label: '计划卷',
            children: (
              <Space orientation="vertical" size="small" style={{ width: '100%' }}>
                {planned_volumes.map((vol, idx) => (
                  <div key={idx} style={{ display: 'flex', justifyContent: 'space-between' }}>
                    <Text>{vol.path}</Text>
                    <Text type="secondary">{vol.filesystem ?? '-'} · {vol.label || '无标签'}</Text>
                  </div>
                ))}
              </Space>
            ),
          },
        ]}
      />

      <Divider style={{ margin: '8px 0' }} />

      <Text strong>子任务进度</Text>
      <Space orientation="vertical" style={{ width: '100%' }} size="small">
        {task_ids.map((id, idx) => (
          <BatchChildTaskRow
            key={id}
            index={idx}
            taskId={id}
            plannedVolume={planned_volumes[idx]}
            onViewSnapshot={onViewSnapshot}
          />
        ))}
      </Space>

      <Space wrap>
        {mode === 'done' && (
          <Alert
            type={errorCount > 0 ? 'warning' : 'success'}
            showIcon
            title={errorCount > 0 ? `批次完成：${doneCount - errorCount} 成功，${errorCount} 失败` : '批次全部完成'}
            style={{ width: '100%' }}
          />
        )}
        {isActive && (
          <Button
            danger
            icon={<StopOutlined />}
            disabled={cancelRequested}
            onClick={onCancel}
          >
            取消批次
          </Button>
        )}
        <Button onClick={onClose}>{isActive ? '后台运行' : '关闭'}</Button>
      </Space>
    </Space>
  )
}

interface BatchChildTaskRowProps {
  index: number
  taskId: string
  plannedVolume: { path: string; filesystem?: string; label?: string | null }
  onViewSnapshot: (snapshotId: string) => void
}

function BatchChildTaskRow({ index, taskId, plannedVolume, onViewSnapshot }: BatchChildTaskRowProps) {
  const { task } = useTaskEvents(taskId, true)
  if (!task) {
    return (
      <div style={{ border: '1px solid #f0f0f0', borderRadius: 6, padding: 12 }}>
        <Space style={{ justifyContent: 'space-between', width: '100%' }}>
          <Text strong>卷 {index + 1}: {plannedVolume.path}</Text>
          <Tag>待处理</Tag>
        </Space>
        <Progress percent={0} status="active" showInfo={false} />
      </div>
    )
  }

  return (
    <div style={{ border: '1px solid #f0f0f0', borderRadius: 6, padding: 12 }}>
      <Space style={{ justifyContent: 'space-between', width: '100%' }}>
        <Text strong>卷 {index + 1}: {plannedVolume.path}</Text>
        <StatusTag status={task.status} />
      </Space>
      <div style={{ marginTop: 8 }}>
        <ProgressSection task={task} />
      </div>
      {task.status === 'done' && task.result?.snapshot_id && (
        <div style={{ marginTop: 8 }}>
          <Space orientation="vertical" size="small" style={{ width: '100%' }}>
            <Button type="link" style={{ padding: 0 }} onClick={() => onViewSnapshot(task.result!.snapshot_id!)}>
              查看快照
            </Button>
            <CollectSmartHint snapshotId={task.result!.snapshot_id!} />
          </Space>
        </div>
      )}
    </div>
  )
}

function BatchStatusTag({ status }: { status: 'running' | 'partial' | 'done' }) {
  if (status === 'done') {
    return <Tag color="success" icon={<CheckCircleOutlined />}>完成</Tag>
  }
  if (status === 'partial') {
    return <Tag color="warning" icon={<CloseCircleOutlined />}>部分完成</Tag>
  }
  return <Tag color="processing">运行中</Tag>
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

function PreflightSection({ path }: { path: string }) {
  const { data: preflight, isLoading, error } = useCollectPreflight(path)

  if (!path.trim()) {
    return (
      <Alert
        type="info"
        showIcon
        icon={<SafetyOutlined />}
        title="依赖检查"
        description="输入采集路径后将自动检查写权限与 smartctl 可用性。"
      />
    )
  }

  if (isLoading) {
    return (
      <Alert
        type="info"
        showIcon
        icon={<SafetyOutlined />}
        title="依赖检查"
        description="正在检查…"
      />
    )
  }

  if (error) {
    return (
      <Alert
        type="warning"
        showIcon
        icon={<WarningOutlined />}
        title="依赖检查失败"
        description={error instanceof Error ? error.message : String(error)}
      />
    )
  }

  if (!preflight) {
    return null
  }

  const items = [
    {
      ok: preflight.writable,
      label: '写权限',
      okText: '可写',
      failText: '不可写',
    },
    {
      ok: preflight.smartctl_available,
      label: 'smartctl',
      okText: preflight.smartctl_path ? `可用（${preflight.smartctl_path}）` : '可用',
      failText: '不可用',
    },
  ]

  return (
    <Alert
      type={preflight.writable ? 'success' : 'error'}
      showIcon
      icon={<SafetyOutlined />}
      title="依赖检查"
      description={
        <Space orientation="vertical" style={{ width: '100%' }} size="small">
          <Space wrap>
            {items.map((item) => (
              <Tag
                key={item.label}
                color={item.ok ? 'success' : 'error'}
                icon={item.ok ? <CheckCircleOutlined /> : <CloseCircleOutlined />}
              >
                {item.label}：{item.ok ? item.okText : item.failText}
              </Tag>
            ))}
          </Space>
          {!preflight.writable && (
            <Text type="danger">路径不可写，无法开始采集。请检查路径是否存在以及当前用户是否有写入权限。</Text>
          )}
          {!preflight.smartctl_available && (
            <Text type="secondary">
              smartctl 不可用。如需采集 SMART，请安装 smartmontools，或设置环境变量 <Text code>CLDM_SMARTCTL</Text> 指向 smartctl 路径。
              USB 桥接盘通常需要加 <Text code>-d sat</Text> 参数。
            </Text>
          )}
          {preflight.warnings.length > 0 && (
            <Space orientation="vertical" size={0}>
              {preflight.warnings.map((w, idx) => (
                <Text key={idx} type="warning">
                  <WarningOutlined /> {w}
                </Text>
              ))}
            </Space>
          )}
        </Space>
      }
    />
  )
}

/**
 * 采集完成后检查这块盘的 SMART 是否读取失败（disk 详情的 smart_error 正好指向
 * 本次快照时才提示，避免老快照的失败误报）。给出可操作的建议，而不是默默略过。
 */
function CollectSmartHint({ snapshotId }: { snapshotId: string }) {
  const { data: snap } = useSnapshot(snapshotId)
  const { data: disk } = useDisk(snap?.disk_id)
  const err = disk?.smart_error
  if (!err || err.snapshot_id !== snapshotId) return null
  return (
    <Alert
      type="warning"
      showIcon
      title="SMART 未能读取（不影响元数据采集）"
      description={
        <Space orientation="vertical" size={4} style={{ width: '100%' }}>
          <Text>{err.smart_error}</Text>
          <Text type="secondary">
            常见处理：安装 smartmontools；以管理员/root 身份运行；USB 桥接盘加 <Text code>-d sat</Text>。
            盘接好后可在磁盘页点「现在读取 SMART」重试。
          </Text>
        </Space>
      }
    />
  )
}

function isTerminal(status: Task['status']): boolean {
  return status === 'done' || status === 'error' || status === 'cancelled'
}

/**
 * 本机盘/卷快选（GET /api/disks/attached）：
 * - 枚举成功且有已挂载卷 → 下拉选择，选中自动填采集路径（并回填序列号）；
 * - 枚举成功但无挂载卷 → 列出磁盘（禁用项）并提示手输路径；
 * - 枚举失败（available=false）→ 保持手输，展示原因。
 */
function AttachedDiskQuickPick({ onPick }: { onPick: (path: string, serial?: string) => void }) {
  const { data, isLoading } = useAttachedDisks(true)

  if (isLoading) {
    return (
      <Form.Item label="本机磁盘">
        <Select loading placeholder="正在枚举本机磁盘…" style={{ width: '100%' }} />
      </Form.Item>
    )
  }

  if (!data) return null

  if (!data.available) {
    return (
      <Form.Item label="本机磁盘">
        <Alert
          type="info"
          showIcon
          title={`无法枚举本机磁盘（${data.reason ?? '未知原因'}），请手动输入采集路径。`}
        />
      </Form.Item>
    )
  }

  // 盘 → 已挂载卷的级联选项；整盘无挂载点的以禁用项呈现
  const options = data.items.map((disk: AttachedDisk) => ({
    label: `${disk.device} · ${disk.model || '未知型号'} · ${formatFileSize(disk.size_bytes)}`,
    title: disk.serial || undefined,
    options:
      disk.volumes.length > 0
        ? disk.volumes.map((v) => ({
            value: `${disk.device}\u0000${v.path}`,
            label: `${v.path}（${v.filesystem || '未知文件系统'}${v.label ? ` · ${v.label}` : ''}）`,
          }))
        : [{ value: `${disk.device}\u0000__none__`, label: '（无已挂载分区）', disabled: true }],
  }))
  const mountedCount = data.items.reduce((n, d) => n + d.volumes.length, 0)

  return (
    <Form.Item
      label="本机磁盘快选"
      extra={
        mountedCount > 0
          ? '选择已挂载的卷后自动填入下方路径；也可直接手输。'
          : '本机磁盘已枚举，但没有发现已挂载的卷（如 WSL 裸盘）；请手动输入采集路径。'
      }
    >
      <Select
        placeholder="选择本机磁盘 / 挂载点…"
        style={{ width: '100%' }}
        allowClear
        showSearch
        optionFilterProp="label"
        options={options}
        value={undefined}
        onChange={(value: unknown) => {
          if (!value) return
          const found = data.items
            .flatMap((d) => d.volumes.map((v) => ({ key: `${d.device}\u0000${v.path}`, path: v.path, serial: d.serial || undefined })))
            .find((o) => o.key === value)
          if (found) onPick(found.path, found.serial)
        }}
      />
    </Form.Item>
  )
}
