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
  Segmented,
  App,
} from 'antd'
import {
  ScanOutlined,
  ImportOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  DatabaseOutlined,
  FolderOpenOutlined,
} from '@ant-design/icons'
import { useQueryClient } from '@tanstack/react-query'
import { useImportScan, useCreateImport, useImportDb } from '../api/hooks'
import { apiErrorDetail, getTask, type ImportCandidate, type Task } from '../api/client'
import { formatFileSize, formatDateTime } from '../utils/format'

const { Text } = Typography

const LAST_IMPORT_PATH_KEY = 'cldm-import-last-path'
const LAST_IMPORT_DB_PATH_KEY = 'cldm-import-last-db-path'
const DEFAULT_PATH = '/tmp/opencode'

type Mode = 'select' | 'progress' | 'done'
/** 导入方式：legacy = v1 快照目录扫描导入；db = 就地登记已封库 snapshot.db */
type ImportKind = 'legacy' | 'db'

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
  const { message } = App.useApp()
  const qc = useQueryClient()
  const [kind, setKind] = useState<ImportKind>('legacy')
  const [path, setPath] = useState(() => localStorage.getItem(LAST_IMPORT_PATH_KEY) || DEFAULT_PATH)
  const [mode, setMode] = useState<Mode>('select')
  const [selectedDirs, setSelectedDirs] = useState<string[]>([])
  const [force, setForce] = useState(false)
  const [jobs, setJobs] = useState<Job[]>([])
  const [submitErrors, setSubmitErrors] = useState<string[]>([])

  // —— db 登记流程状态 ——
  const [dbPath, setDbPath] = useState(() => localStorage.getItem(LAST_IMPORT_DB_PATH_KEY) || '')
  const [dbError, setDbError] = useState<{ message: string; hint?: string } | null>(null)
  const importDbMutation = useImportDb()

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
    setDbError(null)
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

  /** 就地登记 snapshot.db：成功（含幂等）→ toast + 跳快照详情；失败 → 分类可操作提示。 */
  const submitDbImport = async () => {
    const p = dbPath.trim()
    if (!p || importDbMutation.isPending) return
    setDbError(null)
    try {
      const res = await importDbMutation.mutateAsync(p)
      localStorage.setItem(LAST_IMPORT_DB_PATH_KEY, p)
      qc.invalidateQueries({ queryKey: ['snapshots'] })
      qc.invalidateQueries({ queryKey: ['volumes'] })
      message.success(
        res.created
          ? `已登记快照：${res.volume_id} / ${res.snapshot_id}`
          : `该快照此前已登记过（幂等），无需重复导入：${res.snapshot_id}`
      )
      handleClose()
      navigate(`/snapshots?snapshot=${encodeURIComponent(res.snapshot_id)}`)
    } catch (e) {
      setDbError(describeDbImportError(e))
    }
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
        const msg = apiErrorDetail(e)
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
        <Segmented<ImportKind>
          block
          value={kind}
          onChange={(v) => setKind(v)}
          options={[
            { value: 'legacy', label: '旧版快照目录（v1）', icon: <FolderOpenOutlined /> },
            { value: 'db', label: '登记已封库 snapshot.db', icon: <DatabaseOutlined /> },
          ]}
        />

        {kind === 'db' && (
          <>
            <Alert
              type="info"
              showIcon
              title="就地登记已封库的快照库"
              description={
                <Space orientation="vertical" size={4}>
                  <Text>指向一个<strong>已封库</strong>的 snapshot.db 文件（典型位置：卷目录/时间戳目录/snapshot.db）。</Text>
                  <Text type="secondary">只登记引用原文件，不会移动或复制数据；路径需在后端 CLDM_IMPORT_ROOTS 白名单内（若已设置）。重复登记同一文件是安全的（幂等）。</Text>
                </Space>
              }
            />
            <Space.Compact style={{ width: '100%' }}>
              <Input
                placeholder="输入 snapshot.db 绝对路径，例如 /mnt/backup/vol_XX/20260914T120000/snapshot.db"
                value={dbPath}
                onChange={(e) => setDbPath(e.target.value)}
                onPressEnter={submitDbImport}
              />
              <Button
                type="primary"
                icon={<ImportOutlined />}
                loading={importDbMutation.isPending}
                disabled={!dbPath.trim()}
                onClick={submitDbImport}
              >
                登记
              </Button>
            </Space.Compact>
            {dbError && (
              <Alert
                type="error"
                showIcon
                title={dbError.message}
                description={dbError.hint}
                closable
                onClose={() => setDbError(null)}
              />
            )}
          </>
        )}

        {kind === 'legacy' && mode === 'select' && (
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

        {kind === 'legacy' && (mode === 'progress' || mode === 'done') && (
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

/** 把 import-db 的 400 detail 分类为「错误原文 + 怎么改」的可操作提示。 */
function describeDbImportError(e: unknown): { message: string; hint?: string } {
  const detail = apiErrorDetail(e)
  if (detail.includes('未封库') || detail.includes('sealed')) {
    return {
      message: detail,
      hint: '该 snapshot.db 尚未封库。请先在旧版工具里完成封存（seal），或确认路径指向的是已封库的快照库后重试。',
    }
  }
  if (detail.includes('白名单') || detail.includes('导入根')) {
    return {
      message: detail,
      hint: '该路径不在允许的导入根内。两种改法：① 把文件所在目录加入后端环境变量 CLDM_IMPORT_ROOTS（多个路径用系统路径分隔符“;”（Windows）或“:”（Linux/macOS）分隔）并重启服务；② 把 snapshot.db 移动/拷贝到已允许的导入根目录下。',
    }
  }
  if (detail.includes('绝对路径')) {
    return { message: detail, hint: '请填写绝对路径：以 /（Linux/macOS）或盘符开头（如 D:\\backup\\...）。' }
  }
  if (detail.includes('不存在') || detail.includes('不是文件')) {
    return { message: detail, hint: '请检查路径拼写：这里应指向单个 snapshot.db 文件（通常位于 卷目录/时间戳目录/ 下），而不是目录。' }
  }
  return { message: detail }
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
