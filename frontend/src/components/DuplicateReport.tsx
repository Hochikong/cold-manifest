import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Alert,
  App,
  Button,
  Card,
  Col,
  Collapse,
  Empty,
  Row,
  Segmented,
  Skeleton,
  Space,
  Statistic,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import {
  FileTextOutlined,
  LoadingOutlined,
  SafetyOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import ErrorAlert from './ErrorAlert'
import CursorPager from './CursorPager'
import ResizableTable from './ResizableTable'
import EllipsisText from './EllipsisText'
import { useRowContextMenu } from '../hooks/useRowContextMenu'
import { useShowApiError } from '../utils/apiError'
import { formatFileSize, formatNumber } from '../utils/format'
import { dirNameOf } from '../utils/path'
import {
  getDuplicates,
  submitHashTask,
  type DuplicateItem,
  type DuplicateMode,
} from '../api/client'
import { useSettings, useSnapshot, useTaskEvents, useTasks } from '../api/hooks'

const { Text } = Typography

const MIN_SIZE_OPTIONS = [
  { label: '0 B', value: 0 },
  { label: '1 MiB', value: 1024 * 1024 },
  { label: '10 MiB', value: 10 * 1024 * 1024 },
  { label: '100 MiB', value: 100 * 1024 * 1024 },
]

const DEFAULT_PAGE_SIZE = 50

const MODE_OPTIONS: { value: DuplicateMode; label: string }[] = [
  { value: 'content', label: '按内容（严谨）' },
  { value: 'fingerprint', label: '按指纹（较快）' },
  { value: 'name', label: '按文件名（秒级）' },
]

const MODE_LABEL: Record<DuplicateMode, string> = {
  content: '按内容',
  fingerprint: '按指纹',
  name: '按文件名',
}

const MODE_HINT: Record<DuplicateMode, string> = {
  content: '按 SHA-256 完整哈希逐字节比对，重复组按浪费空间从大到小排列。',
  fingerprint: '按「大小 + 首/中/尾 64KB 抽样指纹」分组，重复组按浪费空间从大到小排列。',
  name: '按文件名分组（不区分大小写），零读盘、秒级完成。',
}

/** 置信度标注：每档必须在显著位置说清结论的可靠程度。 */
const CONFIDENCE: Record<
  DuplicateMode,
  { type: 'success' | 'info' | 'warning'; icon: React.ReactNode; title: string; text: string }
> = {
  content: {
    type: 'success',
    icon: <SafetyOutlined />,
    title: '逐字节验证',
    text: '组内文件已按完整哈希比对，内容完全一致，可作为删除依据。',
  },
  fingerprint: {
    type: 'info',
    icon: <ThunderboltOutlined />,
    title: '高度可能重复',
    text: '按首/中/尾 64KB 抽样指纹匹配，未逐字节验证；删除前建议再用「按内容」档复核。',
  },
  name: {
    type: 'warning',
    icon: <FileTextOutlined />,
    title: '仅同名匹配',
    text: '内容可能不同，不能作为删除依据。',
  },
}

function isPolicyError(error: unknown): boolean {
  if (!error || typeof error !== 'object') return false
  const e = error as { response?: { data?: { detail?: string } }; message?: string }
  const detail = e.response?.data?.detail || e.message || ''
  return /hash_policy.*full|完整哈希|full.*hash|hash.*full/i.test(detail)
}

export default function DuplicateReport({ snapshotId }: { snapshotId: string }) {
  const { message } = App.useApp()
  const showApiError = useShowApiError()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const rowMenu = useRowContextMenu()
  const { data: settings } = useSettings()
  const { data: snapshot } = useSnapshot(snapshotId)

  const [userMode, setUserMode] = useState<DuplicateMode | null>(null)
  const [minSize, setMinSize] = useState<number>(1024 * 1024)
  // cursor 栈分页：栈底 null = 第一页；上一页弹栈，下一页压入 next_cursor
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE)
  const [cursorStack, setCursorStack] = useState<(string | null)[]>([null])
  const pageIndex = cursorStack.length - 1
  const tableTopRef = useRef<HTMLDivElement | null>(null)
  const mountedRef = useRef(false)

  // 可用档位以快照库 meta.hash_policy 为准（哈希任务完成后会更新）。
  const hashPolicy = snapshot?.meta?.hash_policy ?? snapshot?.hash_policy ?? 'none'
  const availability = useMemo<Record<DuplicateMode, boolean>>(
    () => ({
      content: hashPolicy === 'full',
      fingerprint: hashPolicy === 'full' || hashPolicy === 'sampled',
      name: true,
    }),
    [hashPolicy]
  )
  const autoMode: DuplicateMode = availability.content
    ? 'content'
    : availability.fingerprint
      ? 'fingerprint'
      : 'name'
  const mode = userMode ?? autoMode
  const ready = snapshot != null

  const duplicatesQuery = useQuery({
    queryKey: ['duplicates', snapshotId, mode, minSize, pageSize, cursorStack[pageIndex]],
    queryFn: () =>
      getDuplicates(snapshotId, {
        mode,
        min_size: minSize,
        limit: pageSize,
        cursor: cursorStack[pageIndex] ?? undefined,
      }),
    enabled: ready && availability[mode],
    staleTime: 30_000,
  })
  const page = duplicatesQuery.data ?? null
  const items = useMemo(() => page?.items ?? [], [page])

  // 快照 / 档位 / 门槛变化 → 重置回第一页（渲染期同步，避免 effect 级联）
  const filterKey = `${snapshotId}|${mode}|${minSize}`
  const [syncedFilterKey, setSyncedFilterKey] = useState(filterKey)
  if (filterKey !== syncedFilterKey) {
    setSyncedFilterKey(filterKey)
    setCursorStack([null])
  }

  // 翻页 / 改行数后滚回表格顶部（首次挂载不滚）
  useEffect(() => {
    if (!mountedRef.current) {
      mountedRef.current = true
      return
    }
    tableTopRef.current?.scrollIntoView({ block: 'start', behavior: 'smooth' })
  }, [pageIndex, pageSize])

  const goNext = () => {
    if (page?.has_more && page.next_cursor) setCursorStack((s) => [...s, page.next_cursor!])
  }
  const goPrev = () => setCursorStack((s) => (s.length > 1 ? s.slice(0, -1) : s))
  const changePageSize = (n: number) => {
    setPageSize(n)
    setCursorStack([null])
  }

  // 后端仍判档位不可用（meta 陈旧等）：不整页报错，降级为引导面板。
  const policyBlocked = !!duplicatesQuery.error && isPolicyError(duplicatesQuery.error)
  const policyDetail = policyBlocked
    ? ((duplicatesQuery.error as { response?: { data?: { detail?: string } } }).response?.data
        ?.detail ?? null)
    : null
  const showGuide = ready && (policyBlocked || !availability[mode])

  // 跟踪本快照的哈希任务：进行中提示，结束后刷新 meta 与结果。
  const { data: tasksData } = useTasks({ limit: 20 })
  const activeHashTask = useMemo(
    () =>
      tasksData?.items.find(
        (t) =>
          t.type === 'hash' &&
          t.payload?.snapshot_id === snapshotId &&
          (t.status === 'pending' || t.status === 'running' || t.status === 'cancelling')
      ),
    [tasksData, snapshotId]
  )

  const activeHashIdRef = useRef<string | null>(null)
  useEffect(() => {
    if (activeHashTask) {
      activeHashIdRef.current = activeHashTask.id
      return
    }
    if (activeHashIdRef.current) {
      activeHashIdRef.current = null
      // 任务结束：meta 与查重结果都可能变化，全部重新拉取
      qc.invalidateQueries({ queryKey: ['snapshot', snapshotId] })
      qc.invalidateQueries({ queryKey: ['duplicates', snapshotId] })
    }
  }, [activeHashTask, qc, snapshotId])

  const hashMutation = useMutation({
    mutationFn: () =>
      submitHashTask(snapshotId, { algo: 'sha256', policy: 'sampled', scope: 'candidates' }),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ['tasks'] })
      setSubmittedTaskId(res.task_id)
      message.success(
        `指纹任务已创建（${res.task_id.slice(0, 12)}…），请到任务页查看进度；完成后本页即可切换「按指纹」档`
      )
    },
    onError: (e) => {
      const status = (e as { response?: { status?: number } }).response?.status
      if (status === 409) {
        message.info('该快照已有哈希任务在进行中，完成后即可切换档位')
      } else {
        showApiError(e, '指纹任务创建失败')
      }
    },
  })

  // 本页提交的任务可能秒级完成（赶不到 4s 轮询）：直接订阅其事件，结束即刷新。
  const [submittedTaskId, setSubmittedTaskId] = useState<string | null>(null)
  const { task: submittedTask } = useTaskEvents(submittedTaskId ?? undefined, !!submittedTaskId)
  const handledTaskRef = useRef<string | null>(null)
  useEffect(() => {
    const t = submittedTask
    if (!t || handledTaskRef.current === t.id) return
    if (t.status === 'done' || t.status === 'error' || t.status === 'cancelled') {
      handledTaskRef.current = t.id
      qc.invalidateQueries({ queryKey: ['snapshot', snapshotId] })
      qc.invalidateQueries({ queryKey: ['duplicates', snapshotId] })
    }
  }, [submittedTask, qc, snapshotId])

  if (!ready) {
    return (
      <Card title="重复文件" size="small" style={{ marginBottom: 16 }}>
        <Skeleton active paragraph={{ rows: 6 }} />
      </Card>
    )
  }

  if (duplicatesQuery.error && !policyBlocked) {
    return <ErrorAlert error={duplicatesQuery.error} />
  }

  const dataRoot = settings?.data_root || ''
  const hashCommand = `cldm hash ${snapshotId} --policy full${dataRoot ? ` --data-root "${dataRoot}"` : ''}`
  const confidence = CONFIDENCE[mode]
  const isNameMode = mode === 'name'
  const fetchingDuplicates = duplicatesQuery.isFetching
  const showSkeleton = fetchingDuplicates && items.length === 0 && !policyBlocked
  const nameLoadedBytes = items.reduce((acc, it) => acc + (it.size_bytes || 0), 0)

  return (
    <div>
      <Card title="重复文件" size="small" style={{ marginBottom: 16 }}>
        <Space orientation="vertical" size="middle" style={{ width: '100%' }}>
          <Text type="secondary">{MODE_HINT[mode]}</Text>
          <Space wrap size="large">
            <Space size={8}>
              <Text type="secondary">查重档位</Text>
              <Segmented<DuplicateMode>
                value={mode}
                onChange={(v) => setUserMode(v)}
                options={MODE_OPTIONS}
              />
            </Space>
            {!availability[mode] && (
              <Space size={4}>
                <Tag color="warning">
                  {mode === 'content' ? '该档需要完整哈希' : '该档需要哈希指纹'}
                </Tag>
                <Button type="link" size="small" onClick={() => setUserMode(autoMode)}>
                  切到「{MODE_LABEL[autoMode]}」
                </Button>
              </Space>
            )}
            <Space size={8}>
              <Text type="secondary">最小文件大小</Text>
              <Segmented<number>
                value={minSize}
                onChange={setMinSize}
                options={MIN_SIZE_OPTIONS}
              />
            </Space>
          </Space>
          {activeHashTask && (
            <Alert
              type="info"
              showIcon
              icon={<LoadingOutlined spin />}
              title="哈希任务进行中 — 完成后本页自动更新"
              action={
                <Button size="small" onClick={() => navigate('/tasks')}>
                  去任务页
                </Button>
              }
            />
          )}
        </Space>
      </Card>

      {showGuide ? (
        <Card size="small" style={{ marginBottom: 16 }}>
          <Space orientation="vertical" size="middle" style={{ width: '100%' }}>
            <Alert
              type="warning"
              showIcon
              title={
                policyBlocked || mode === 'content' ? '「按内容」需要完整哈希' : '「按指纹」需要先算哈希'
              }
              description={
                policyDetail ||
                (mode === 'content'
                  ? '当前快照没有完整哈希，无法逐字节比对。可先用「按文件名」秒级查重，或给候选文件算指纹后用「按指纹」档。'
                  : '当前快照没有任何哈希。可先用「按文件名」秒级查重，或一键给候选文件算指纹。')
              }
            />
            <Space wrap>
              {!availability.fingerprint && (
                <Button
                  type="primary"
                  icon={<ThunderboltOutlined />}
                  loading={hashMutation.isPending}
                  disabled={!!activeHashTask}
                  onClick={() => hashMutation.mutate()}
                >
                  对候选文件算指纹
                </Button>
              )}
              {availability.fingerprint && mode !== 'fingerprint' && (
                <Button type="primary" onClick={() => setUserMode('fingerprint')}>
                  切到「按指纹」
                </Button>
              )}
              {mode !== 'name' && (
                <Button onClick={() => setUserMode('name')}>切到「按文件名」</Button>
              )}
            </Space>
            <Text type="secondary">
              指纹只对「大小重复」的候选文件计算（首/中/尾 64KB 抽样），通常秒级到分钟级，无需整盘读取；完成后即可切换「按指纹」档。
            </Text>
            <Collapse
              ghost
              items={[
                {
                  key: 'full-hash',
                  label: '仍要完整哈希？先了解代价',
                  children: (
                    <Space orientation="vertical" size="small" style={{ width: '100%' }}>
                      <Text>
                        完整哈希需逐字节读取源文件：USB 机械盘读满 1TB 约需 2–3
                        小时；完成后本页「按内容」档可用。
                      </Text>
                      <Text code copyable>
                        {hashCommand}
                      </Text>
                    </Space>
                  ),
                },
              ]}
            />
          </Space>
        </Card>
      ) : (
        <>
          <Alert
            type={confidence.type}
            showIcon
            icon={confidence.icon}
            title={confidence.title}
            description={confidence.text}
            style={{ marginBottom: 16 }}
            action={
              isNameMode && !availability.fingerprint ? (
                <Button
                  size="small"
                  icon={<ThunderboltOutlined />}
                  loading={hashMutation.isPending}
                  disabled={!!activeHashTask}
                  onClick={() => hashMutation.mutate()}
                >
                  对候选文件算指纹
                </Button>
              ) : undefined
            }
          />

          <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
            <Col xs={24} sm={8}>
              <Card size="small">
                <Statistic title="重复组数" value={page?.duplicate_groups ?? 0} suffix="组" />
              </Card>
            </Col>
            <Col xs={24} sm={8}>
              <Card size="small">
                {isNameMode ? (
                  <Statistic
                    title="同名文件总大小（本页）"
                    value={formatFileSize(nameLoadedBytes)}
                  />
                ) : (
                  <Statistic
                    title="可回收空间"
                    value={formatFileSize(page?.total_wasted_bytes ?? 0)}
                  />
                )}
              </Card>
            </Col>
            <Col xs={24} sm={8}>
              <Card size="small">
                <Statistic
                  title={isNameMode ? '已扫描文件数' : '已哈希文件数'}
                  value={page?.hashed_files ?? 0}
                  suffix="个"
                />
              </Card>
            </Col>
          </Row>

          <Card size="small">
            {showSkeleton ? (
              <Skeleton active paragraph={{ rows: 6 }} />
            ) : items.length ? (
              <>
                <div ref={tableTopRef} />
                <ResizableTable<DuplicateItem>
                  tableId="duplicates"
                  rowKey={(r) => r.hash_hex ?? r.name ?? String(r.size_bytes)}
                  size="small"
                  tableLayout="fixed"
                  pagination={false}
                  loading={fetchingDuplicates}
                  scroll={{ x: 'max-content' }}
                  expandable={{
                    expandedRowRender: (record) => (
                      <Space orientation="vertical" style={{ width: '100%' }}>
                        {record.paths.map((p, idx) => (
                          <div
                            key={idx}
                            style={{ cursor: 'context-menu' }}
                            onContextMenu={(e) =>
                              rowMenu.open(e, { path: p, snapshotId, openDirPath: dirNameOf(p) })
                            }
                          >
                            <Text>
                              <FileTextOutlined style={{ marginRight: 8 }} />
                              {p}
                            </Text>
                          </div>
                        ))}
                        {record.paths_truncated && (
                          <Text type="warning">仅列出前 20 条路径，完整清单请用 CLI 导出 CSV。</Text>
                        )}
                      </Space>
                    ),
                    rowExpandable: () => true,
                  }}
                  columns={[
                    ...(isNameMode
                      ? [
                          {
                            title: '文件名',
                            dataIndex: 'name',
                            width: 220,
                            ellipsis: true,
                            render: (v: string) => <EllipsisText value={v} code />,
                          },
                        ]
                      : [
                          {
                            title: '文件大小',
                            dataIndex: 'size_bytes',
                            width: 140,
                            render: (v: number) => formatFileSize(v),
                          },
                        ]),
                    {
                      title: '重复份数',
                      dataIndex: 'count',
                      width: 110,
                      render: (v: number) => formatNumber(v),
                    },
                    ...(isNameMode
                      ? [
                          {
                            title: '总大小',
                            dataIndex: 'size_bytes',
                            width: 140,
                            render: (v: number) => formatFileSize(v),
                          },
                        ]
                      : [
                          {
                            title: '浪费空间',
                            dataIndex: 'wasted_bytes',
                            width: 140,
                            render: (v: number) => formatFileSize(v),
                          },
                          ...(mode === 'fingerprint'
                            ? [
                                {
                                  title: '验证',
                                  dataIndex: 'verified',
                                  width: 120,
                                  render: (v: boolean | undefined) =>
                                    v ? (
                                      <Tag color="success">完整哈希</Tag>
                                    ) : (
                                      <Tag color="gold">抽样指纹</Tag>
                                    ),
                                },
                              ]
                            : []),
                          {
                            title: '哈希',
                            dataIndex: 'hash_hex',
                            ellipsis: true,
                            render: (v: string) => <EllipsisText value={v} code />,
                          },
                        ]),
                    {
                      title: '路径摘要',
                      key: 'paths',
                      ellipsis: true,
                      render: (_: unknown, record: DuplicateItem) => {
                        const first = record.paths[0] || '-'
                        const rest = record.count - 1
                        return (
                          <Tooltip title={first} placement="topLeft" mouseEnterDelay={0.3}>
                            <Text style={{ display: 'block', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                              {first}
                              {rest > 0 ? (
                                <Text type="secondary"> 等 {formatNumber(rest)} 条</Text>
                              ) : null}
                              {record.paths_truncated ? '（仅列部分）' : null}
                            </Text>
                          </Tooltip>
                        )
                      },
                    },
                  ]}
                  dataSource={items}
                />
                <CursorPager
                  pageSize={pageSize}
                  onPageSizeChange={changePageSize}
                  canPrev={pageIndex > 0}
                  onPrev={goPrev}
                  canNext={page?.has_more ?? false}
                  onNext={goNext}
                  hint={`第 ${pageIndex + 1} 页 · 本页 ${items.length} 组`}
                />
              </>
            ) : (
              <Empty
                description={
                  isNameMode
                    ? `没有同名且 ≥ ${formatFileSize(minSize)} 的文件`
                    : `没有 ≥ ${formatFileSize(minSize)} 的重复文件`
                }
              />
            )}
          </Card>
        </>
      )}
      {rowMenu.element}
    </div>
  )
}
