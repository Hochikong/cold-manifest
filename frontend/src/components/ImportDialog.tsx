import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Modal,
  Input,
  Button,
  Table,
  Checkbox,
  Progress,
  Alert,
  Space,
  Typography,
  Tag,
  Spin,
  Empty,
} from 'antd'
import { ScanOutlined, ImportOutlined, CheckCircleOutlined, CloseCircleOutlined } from '@ant-design/icons'
import { useQueryClient } from '@tanstack/react-query'
import { useImportScan, useCreateImport } from '../api/hooks'
import { getTask, type ImportCandidate, type Task } from '../api/client'
import { formatFileSize, formatDateTime } from '../utils/format'

const { Text } = Typography

const LAST_IMPORT_PATH_KEY = 'cldm-import-last-path'
const DEFAULT_PATH = '/tmp/opencode'

type Mode = 'select' | 'progress' | 'done'

interface Job {
  snapshot_dir: string
  volume_id: string
  task_id: string | null
  status: Task['status'] | 'submitting'
  progress: number | null
  message: string | null
  error: string | null
  result: Task['result']
}

interface ImportDialogProps {
  open: boolean
  onClose: () => void
}

export default function ImportDialog({ open, onClose }: ImportDialogProps) {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [path, setPath] = useState(() => localStorage.getItem(LAST_IMPORT_PATH_KEY) || DEFAULT_PATH)
  const [mode, setMode] = useState<Mode>('select')
  const [selectedDirs, setSelectedDirs] = useState<string[]>([])
  const [force, setForce] = useState(false)
  const [jobs, setJobs] = useState<Job[]>([])
  const [submitErrors, setSubmitErrors] = useState<string[]>([])
  const jobsRef = useRef<Job[]>(jobs)
  jobsRef.current = jobs

  const {
    data: scanResult,
    isLoading: scanning,
    error: scanError,
    refetch: doScan,
  } = useImportScan(path.trim() || undefined, false)

  const createImportMutation = useCreateImport()

  const candidates = scanResult?.candidates ?? []

  useEffect(() => {
    if (!open) return
    setMode('select')
    setSelectedDirs([])
    setForce(false)
    setJobs([])
    setSubmitErrors([])
  }, [open])

  useEffect(() => {
    if (scanResult?.candidates.length) {
      setSelectedDirs(scanResult.candidates.filter((c) => c.status === 'new').map((c) => c.snapshot_dir))
    }
  }, [scanResult])

  const handleScan = () => {
    localStorage.setItem(LAST_IMPORT_PATH_KEY, path.trim())
    doScan()
  }

  const startImport = async () => {
    const selected = candidates.filter((c) => selectedDirs.includes(c.snapshot_dir))
    if (!selected.length) return

    const initialJobs: Job[] = selected.map((c) => ({
      snapshot_dir: c.snapshot_dir,
      volume_id: c.volume_id,
      task_id: null,
      status: 'submitting',
      progress: null,
      message: null,
      error: null,
      result: null,
    }))
    setJobs(initialJobs)
    setMode('progress')
    setSubmitErrors([])

    const errors: string[] = []
    for (let i = 0; i < selected.length; i++) {
      const c = selected[i]
      try {
        const res = await createImportMutation.mutateAsync({
          snapshot_dir: c.snapshot_dir,
          volume_id: c.volume_id,
          force,
        })
        setJobs((prev) => {
          const next = [...prev]
          next[i] = { ...next[i], task_id: res.task_id, status: 'pending' }
          return next
        })
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e)
        errors.push(`${c.volume_id}: ${msg}`)
        setJobs((prev) => {
          const next = [...prev]
          next[i] = { ...next[i], status: 'error', error: msg }
          return next
        })
      }
    }
    setSubmitErrors(errors)
  }

  useEffect(() => {
    if (mode !== 'progress') return
    let cancelled = false

    const tick = async () => {
      const current = jobsRef.current
      const tasks = await Promise.all(
        current.map(async (job) => {
          if (!job.task_id || (job.status !== 'pending' && job.status !== 'running')) return null
          try {
            return await getTask(job.task_id)
          } catch {
            return null
          }
        })
      )
      if (cancelled) return

      setJobs((prev) => {
        const next = [...prev]
        let changed = false
        tasks.forEach((task, idx) => {
          if (!task) return
          if (
            task.status !== next[idx].status ||
            task.progress !== next[idx].progress ||
            task.message !== next[idx].message ||
            task.error !== next[idx].error
          ) {
            next[idx] = {
              ...next[idx],
              status: task.status,
              progress: task.progress,
              message: task.message,
              error: task.error,
              result: task.result,
            }
            changed = true
          }
        })
        return changed ? next : prev
      })
    }

    tick()
    const timer = window.setInterval(tick, 1000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [mode])

  useEffect(() => {
    if (mode !== 'progress') return
    const allSettled = jobs.length > 0 && jobs.every((j) => j.status === 'done' || j.status === 'error')
    if (allSettled) {
      qc.invalidateQueries({ queryKey: ['snapshots'] })
      qc.invalidateQueries({ queryKey: ['volumes'] })
      qc.invalidateQueries({ queryKey: ['tasks'] })
      setMode('done')
    }
  }, [jobs, mode, qc])

  const handleClose = () => {
    onClose()
  }

  const columns = useMemo(
    () => [
      { title: '卷 ID', dataIndex: 'volume_id', ellipsis: true },
      {
        title: '采集时间',
        dataIndex: 'collect_time',
        render: (v: string) => formatDateTime(v),
        width: 150,
      },
      {
        title: 'tree.csv',
        dataIndex: 'tree_csv_bytes',
        render: (v: number) => formatFileSize(v),
        width: 120,
      },
      {
        title: '状态',
        dataIndex: 'status',
        render: (v: ImportCandidate['status']) =>
          v === 'new' ? <Tag color="blue">新快照</Tag> : <Tag>已导入</Tag>,
        width: 90,
      },
    ],
    []
  )

  const rowSelection = {
    selectedRowKeys: selectedDirs,
    onChange: (keys: React.Key[]) => setSelectedDirs(keys as string[]),
    getCheckboxProps: () => ({
      disabled: mode !== 'select',
    }),
  }

  const activeCount = jobs.filter((j) => j.status === 'pending' || j.status === 'running' || j.status === 'submitting').length
  const doneCount = jobs.filter((j) => j.status === 'done').length
  const errorCount = jobs.filter((j) => j.status === 'error').length

  return (
    <Modal
      title="导入旧快照"
      open={open}
      onCancel={handleClose}
      width={720}
      footer={null}
      destroyOnHidden
    >
      <Space orientation="vertical" style={{ width: '100%' }} size="middle">
        {mode === 'select' && (
          <>
            <Space.Compact style={{ width: '100%' }}>
              <Input
                placeholder="输入目录绝对路径，例如 /tmp/opencode"
                value={path}
                onChange={(e) => setPath(e.target.value)}
                onPressEnter={handleScan}
              />
              <Button icon={<ScanOutlined />} loading={scanning} onClick={handleScan}>
                扫描
              </Button>
            </Space.Compact>

            {scanError && <Alert type="error" showIcon title={scanError instanceof Error ? scanError.message : String(scanError)} />}
            {scanResult?.errors.length ? (
              <Alert
                type="warning"
                showIcon
                title="扫描遇到部分问题"
                description={scanResult.errors.join('；')}
              />
            ) : null}

            {scanning ? (
              <Spin style={{ display: 'block', margin: '32px auto' }} />
            ) : candidates.length ? (
              <>
                <Table
                  rowKey="snapshot_dir"
                  size="small"
                  rowSelection={rowSelection}
                  columns={columns}
                  dataSource={candidates}
                  pagination={false}
                  scroll={{ x: 'max-content' }}
                />
                <Space>
                  <Checkbox checked={force} onChange={(e) => setForce(e.target.checked)}>
                    覆盖已导入（force）
                  </Checkbox>
                </Space>
                <Button
                  type="primary"
                  icon={<ImportOutlined />}
                  disabled={!selectedDirs.length}
                  loading={createImportMutation.isPending}
                  onClick={startImport}
                  block
                >
                  开始导入（{selectedDirs.length} 项）
                </Button>
              </>
            ) : scanResult ? (
              <Empty description="未找到可导入的快照目录" />
            ) : null}
          </>
        )}

        {(mode === 'progress' || mode === 'done') && (
          <>
            {submitErrors.length > 0 && (
              <Alert type="error" showIcon title="部分任务提交失败" description={submitErrors.join('；')} />
            )}
            <Space orientation="vertical" style={{ width: '100%' }}>
              {jobs.map((job) => (
                <div key={job.snapshot_dir} style={{ border: '1px solid #f0f0f0', borderRadius: 6, padding: 12 }}>
                  <Space style={{ justifyContent: 'space-between', width: '100%' }}>
                    <Text strong>{job.volume_id}</Text>
                    {job.status === 'done' && <Tag color="success" icon={<CheckCircleOutlined />}>完成</Tag>}
                    {job.status === 'error' && <Tag color="error" icon={<CloseCircleOutlined />}>失败</Tag>}
                    {job.status === 'running' && <Tag color="processing">运行中</Tag>}
                    {job.status === 'pending' && <Tag>待处理</Tag>}
                    {job.status === 'submitting' && <Tag>提交中</Tag>}
                  </Space>
                  <div style={{ marginTop: 8 }}>
                    {job.status === 'done' ? (
                      <Text type="secondary">{job.message || '导入完成'}</Text>
                    ) : job.status === 'error' ? (
                      <Text type="danger">{job.error || '导入失败'}</Text>
                    ) : job.progress == null ? (
                      <>
                        <Progress percent={0} status="active" showInfo={false} />
                        <Text type="secondary">{formatMessage(job.message)}</Text>
                      </>
                    ) : (
                      <>
                        <Progress percent={Math.round(job.progress * 100)} status="active" />
                        <Text type="secondary">{formatMessage(job.message)}</Text>
                      </>
                    )}
                  </div>
                </div>
              ))}
            </Space>

            {mode === 'done' && (
              <>
                <Alert
                  type={errorCount > 0 ? 'warning' : 'success'}
                  showIcon
                  title={errorCount > 0 ? `完成：${doneCount} 成功，${errorCount} 失败` : `全部完成：${doneCount} 个快照`}
                />
                <Space wrap>
                  {jobs
                    .filter((j) => j.status === 'done' && j.result?.snapshot_id)
                    .map((j) => (
                      <Button
                        key={j.snapshot_dir}
                        type="primary"
                        onClick={() => {
                          handleClose()
                          navigate(`/snapshots?snapshot=${encodeURIComponent(j.result!.snapshot_id!)}`)
                        }}
                      >
                        查看 {j.volume_id}
                      </Button>
                    ))}
                  <Button onClick={handleClose}>关闭</Button>
                </Space>
              </>
            )}

            {mode === 'progress' && (
              <Text type="secondary">{activeCount > 0 ? `剩余 ${activeCount} 个任务进行中…` : '正在收尾…'}</Text>
            )}
          </>
        )}
      </Space>
    </Modal>
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
