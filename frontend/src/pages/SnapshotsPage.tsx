import { useEffect, useMemo, useState, useCallback, useRef } from 'react'
import { useSearchParams, useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import {
  Card,
  Row,
  Col,
  Statistic,
  Tabs,
  Table,
  Tree,
  Breadcrumb,
  Button,
  Space,
  Input,
  InputNumber,
  Select,
  Segmented,
  Typography,
  Empty,
  Spin,
  Skeleton,
  Alert,
  Drawer,
  Tooltip,
  Modal,
  Checkbox,
  Progress,
  App,
} from 'antd'
import type { TableProps } from 'antd'
import {
  FolderOutlined,
  FileOutlined,
  ReloadOutlined,
  DownloadOutlined,
  FileTextOutlined,
  SearchOutlined,
  ArrowLeftOutlined,
  HomeOutlined,
  DeleteOutlined,
  ExclamationCircleOutlined,
  FilterOutlined,
  StarFilled,
  StarOutlined,
  SyncOutlined,
  EditOutlined,
  LinkOutlined,
} from '@ant-design/icons'
import type { DataNode } from 'antd/es/tree'
import ReactECharts from 'echarts-for-react'
import {
  useSnapshots,
  useSnapshot,
  useSnapshotStats,
  useEntries,
  useTree,
  useDu,
  useSearch,
  useDeleteSnapshot,
  useVolumes,
  usePatchSnapshot,
  useBuildIndexTask,
  useTaskEvents,
} from '../api/hooks'
import ErrorAlert from '../components/ErrorAlert'
import EllipsisText from '../components/EllipsisText'
import CursorPager from '../components/CursorPager'
import ResizableTable from '../components/ResizableTable'
import DuplicateReport from '../components/DuplicateReport'
import HashPanel from '../components/HashPanel'
import SkippedPanel from '../components/SkippedPanel'
import OnDiskCopyBadge from '../components/OnDiskCopyBadge'
import { VerifyCopyButton, VerifyCopyResultCard } from '../components/VerifyCopyCard'
import { useRowContextMenu, type RowContextTarget } from '../hooks/useRowContextMenu'
import { formatFileSize, formatDateTime, formatNumber, nsToDate } from '../utils/format'
import { dirNameOf, joinChildPath } from '../utils/path'
import { exportSnapshotUrl, snapshotReportUrl, REPORT_SECTIONS, getTree, apiErrorDetail, type Entry, type TreeDir, type SearchItem, type DeleteSnapshotBlocked, type VerifyCopyReport, type Snapshot } from '../api/client'
import axios from 'axios'

const { Title, Text } = Typography
const { Option } = Select

type SortKey = 'name' | 'size' | 'mtime'
type Order = 'asc' | 'desc'

const DEFAULT_PAGE_SIZE = 50

function getSnapshotDisplayName(snapshot_id: string) {
  const parts = snapshot_id.split('/')
  return parts.length > 1 ? `${parts[0]} / ${parts[1]}` : snapshot_id
}

function toTreeNode(d: TreeDir): TreeNodeData {
  return { key: d.entry_id, title: d.name, entryId: d.entry_id, isLeaf: d.dir_count === 0, icon: <FolderOutlined /> }
}

interface DirChainNode {
  entryId: number
  children: TreeNodeData[]
}

/** 沿路径逐级走 tree 接口定位目录 entry_id；返回沿途节点用于展开目录树。找不到的段停在最后命中层。 */
async function resolveDirChain(snapshotId: string, path: string): Promise<{ entryId: number; nodes: DirChainNode[] }> {
  const segments = path.split('/').filter(Boolean)
  const nodes: DirChainNode[] = []
  let parentId = 0
  let current = await getTree(snapshotId, parentId)
  for (const seg of segments) {
    const found = current.dirs.find((d) => d.name === seg)
    if (!found) break
    parentId = found.entry_id
    current = await getTree(snapshotId, parentId)
    nodes.push({ entryId: parentId, children: current.dirs.map(toTreeNode) })
  }
  return { entryId: parentId, nodes }
}

function toAntSortOrder(active: boolean, order: Order): 'ascend' | 'descend' | null {
  if (!active) return null
  return order === 'asc' ? 'ascend' : 'descend'
}

export default function SnapshotsPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const navigate = useNavigate()
  const { message } = App.useApp()
  const snapshotId = searchParams.get('snapshot') || undefined
  const activeTab = searchParams.get('tab') || 'overview'

  const [volumeFilter, setVolumeFilter] = useState<string | undefined>(undefined)
  const { data: snapshots, isLoading: listLoading, error: listError, refetch: refetchSnapshots } = useSnapshots(volumeFilter)
  // 详情页标题用 label（与列表/对比页同口径）；与 SnapshotOverview 共享同一查询缓存
  const { data: snapshotMeta } = useSnapshot(snapshotId)
  const deleteMutation = useDeleteSnapshot()

  // 置顶状态由列表行自带的 pinned 字段提供（PATCH 后 invalidate ['snapshots'] 刷新）
  const patchSnapshotMutation = usePatchSnapshot()
  const [pinPendingId, setPinPendingId] = useState<string | null>(null)

  const togglePin = async (sid: string, pinned: boolean) => {
    setPinPendingId(sid)
    try {
      await patchSnapshotMutation.mutateAsync({ snapshot_id: sid, body: { pinned } })
      message.success(pinned ? '已置顶' : '已取消置顶')
    } catch (e) {
      message.error(apiErrorDetail(e) || '操作失败')
    } finally {
      setPinPendingId(null)
    }
  }

  const [deleteTarget, setDeleteTarget] = useState<{ snapshot_id: string; displayName: string; pinned: boolean } | null>(null)
  const [deleteOnDisk, setDeleteOnDisk] = useState(false)
  const [deleteForce, setDeleteForce] = useState(false)
  const [deleteBlocked, setDeleteBlocked] = useState<DeleteSnapshotBlocked | null>(null)
  const [deleteError, setDeleteError] = useState<string | null>(null)

  const setTab = (tab: string) => {
    const next = new URLSearchParams(searchParams)
    next.set('tab', tab)
    setSearchParams(next, { replace: true })
  }

  const openDelete = (id: string) => {
    setDeleteTarget({
      snapshot_id: id,
      displayName: getSnapshotDisplayName(id),
      pinned: !!snapshots?.items.find((s) => s.snapshot_id === id)?.pinned,
    })
    setDeleteOnDisk(false)
    setDeleteForce(false)
    setDeleteBlocked(null)
    setDeleteError(null)
  }

  const closeDelete = () => {
    setDeleteTarget(null)
    setDeleteBlocked(null)
    setDeleteError(null)
  }

  const doDelete = async () => {
    if (!deleteTarget) return
    setDeleteError(null)
    try {
      const res = await deleteMutation.mutateAsync({
        snapshot_id: deleteTarget.snapshot_id,
        on_disk: deleteOnDisk ? 'delete' : 'keep',
        force: deleteForce,
      })
      const parts: string[] = ['快照已删除']
      if (res.deleted_host) parts.push('主机目录已清理')
      if (res.deleted_disk === true) parts.push('盘上副本已清理')
      if (res.freed_bytes > 0) parts.push(`释放 ${formatFileSize(res.freed_bytes)}`)
      if (res.diffs_removed.length > 0) parts.push(`级联删除 ${res.diffs_removed.length} 个对比`)
      message.success(parts.join(' · '))
      closeDelete()
      await refetchSnapshots()
      if (snapshotId === deleteTarget.snapshot_id) {
        navigate('/snapshots')
      }
    } catch (e) {
      if (axios.isAxiosError(e) && e.response?.status === 409) {
        // 409 响应体形如 {"detail": {message, diffs, tasks}}；detail 也可能是字符串或其他
        const body = e.response.data as { detail?: unknown } | undefined
        const detail = body?.detail
        if (detail && typeof detail === 'object' && 'message' in (detail as Record<string, unknown>)) {
          const blocked = detail as DeleteSnapshotBlocked
          setDeleteBlocked(blocked)
          setDeleteError(blocked.message)
        } else {
          setDeleteError(typeof detail === 'string' && detail ? detail : '删除被阻塞，请确认相关任务或对比')
        }
      } else {
        setDeleteError(apiErrorDetail(e))
      }
    }
  }

  if (!snapshotId) {
    return (
      <>
        <SnapshotListView
          snapshots={snapshots?.items}
          loading={listLoading}
          error={listError}
          volumeFilter={volumeFilter}
          onVolumeFilterChange={setVolumeFilter}
          pinPendingId={pinPendingId}
          onTogglePin={togglePin}
          onSelect={(id) => {
            const next = new URLSearchParams()
            next.set('snapshot', id)
            next.set('tab', 'overview')
            setSearchParams(next)
          }}
          onDelete={openDelete}
        />
        <DeleteSnapshotModal
          target={deleteTarget}
          onDisk={deleteOnDisk}
          onDiskChange={setDeleteOnDisk}
          force={deleteForce}
          forceChange={setDeleteForce}
          blocked={deleteBlocked}
          error={deleteError}
          loading={deleteMutation.isPending}
          onCancel={closeDelete}
          onConfirm={doDelete}
        />
      </>
    )
  }

  return (
    <div>
      <Space align="center" style={{ marginBottom: 16 }} wrap>
        <Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/snapshots')}>
          返回列表
        </Button>
        <Title level={4} style={{ margin: 0 }}>
          快照：{snapshotMeta?.label || getSnapshotDisplayName(snapshotId)}
        </Title>
        {!!snapshotMeta?.label && <Text type="secondary" code copyable={{ tooltips: ['复制快照 ID', '已复制'] }}>{snapshotId}</Text>}
      </Space>
      <Tabs
        activeKey={activeTab}
        onChange={setTab}
        items={[
          { key: 'overview', label: '概览', children: <SnapshotOverview snapshotId={snapshotId} onDelete={() => openDelete(snapshotId)} /> },
          { key: 'browse', label: '浏览', children: <DirectoryBrowser snapshotId={snapshotId} /> },
          { key: 'duplicates', label: '重复文件', children: <DuplicateReport snapshotId={snapshotId} /> },
          { key: 'search', label: '搜索', children: <SearchPanel snapshotId={snapshotId} /> },
          { key: 'skipped', label: '跳过项', children: <SkippedPanel snapshotId={snapshotId} /> },
          { key: 'export', label: '导出', children: <ExportPanel snapshotId={snapshotId} /> },
        ]}
      />
      <DeleteSnapshotModal
        target={deleteTarget}
        onDisk={deleteOnDisk}
        onDiskChange={setDeleteOnDisk}
        force={deleteForce}
        forceChange={setDeleteForce}
        blocked={deleteBlocked}
        error={deleteError}
        loading={deleteMutation.isPending}
        onCancel={closeDelete}
        onConfirm={doDelete}
      />
    </div>
  )
}

function SnapshotListView({
  snapshots,
  loading,
  error,
  volumeFilter,
  onVolumeFilterChange,
  pinPendingId,
  onTogglePin,
  onSelect,
  onDelete,
}: {
  snapshots?: Snapshot[]
  loading: boolean
  error: unknown
  volumeFilter: string | undefined
  onVolumeFilterChange: (v: string | undefined) => void
  pinPendingId: string | null
  onTogglePin: (sid: string, pinned: boolean) => void
  onSelect: (id: string) => void
  onDelete: (id: string) => void
}) {
  const { data: volumes } = useVolumes()
  const [pinnedOnly, setPinnedOnly] = useState(false)
  const volumeOptions = (volumes?.items ?? [])
    .map((v) => ({ value: v.volume_id, label: `${v.volume_id}（${v.snapshot_count} 个快照）` }))
    .sort((a, b) => a.value.localeCompare(b.value))

  const visibleRows = useMemo(() => {
    if (!pinnedOnly || !snapshots) return snapshots
    return snapshots.filter((s) => s.pinned)
  }, [snapshots, pinnedOnly])

  const columns = [
    {
      title: '',
      key: 'pin',
      width: 48,
      render: (_: unknown, record: { snapshot_id: string; pinned: boolean }) => {
        const pinned = record.pinned
        return (
          <Tooltip title={pinned ? '取消置顶' : '置顶快照'}>
            <Button
              type="text"
              size="small"
              loading={pinPendingId === record.snapshot_id}
              icon={
                pinned ? (
                  <StarFilled style={{ color: '#faad14' }} />
                ) : (
                  <StarOutlined style={{ color: '#bfbfbf' }} />
                )
              }
              onClick={() => onTogglePin(record.snapshot_id, !pinned)}
            />
          </Tooltip>
        )
      },
    },
    {
      title: '快照',
      dataIndex: 'snapshot_id',
      key: 'snapshot_id',
      ellipsis: true,
      render: (v: string, record: Snapshot) =>
        record.label ? (
          // 有昵称时 label 作主标题，快照 ID 退为次要信息
          <Space orientation="vertical" size={0} style={{ display: 'flex' }}>
            <Text strong style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{record.label}</Text>
            <Text type="secondary" code style={{ fontSize: 12, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', display: 'block' }}>{v}</Text>
          </Space>
        ) : (
          <EllipsisText value={v} code />
        ),
    },
    {
      title: '卷',
      dataIndex: 'volume_id',
      key: 'volume_id',
      ellipsis: true,
      width: 220,
      render: (v: string) => <EllipsisText value={v} code />,
    },
    { title: '采集时间', dataIndex: 'collected_at', key: 'collected_at', width: 150, render: (v: string) => formatDateTime(v) },
    { title: '文件数', dataIndex: 'file_count', key: 'file_count', width: 110, align: 'right' as const, render: (v: number) => formatNumber(v) },
    { title: '总大小', dataIndex: 'total_bytes', key: 'total_bytes', width: 120, align: 'right' as const, render: (v: number) => formatFileSize(v) },
    {
      title: '操作',
      key: 'action',
      width: 170,
      render: (_: unknown, record: { snapshot_id: string }) => (
        <Space>
          <Button type="primary" onClick={() => onSelect(record.snapshot_id)}>
            查看
          </Button>
          <Button danger icon={<DeleteOutlined />} onClick={() => onDelete(record.snapshot_id)}>
            删除
          </Button>
        </Space>
      ),
    },
  ]

  return (
    <div>
      <Space align="center" style={{ marginTop: 0, marginBottom: 16 }} wrap>
        <Title level={4} style={{ margin: 0 }}>快照</Title>
        <Select
          placeholder="按卷筛选"
          allowClear
          showSearch
          optionFilterProp="label"
          style={{ minWidth: 260 }}
          value={volumeFilter}
          onChange={(v) => onVolumeFilterChange(v)}
          options={volumeOptions}
        />
        <Tooltip title="只显示已置顶的快照">
          <Checkbox
            checked={pinnedOnly}
            onChange={(e) => setPinnedOnly(e.target.checked)}
          >
            <StarFilled style={{ color: '#faad14', marginRight: 4 }} />
            只看置顶
          </Checkbox>
        </Tooltip>
        {volumeFilter && (
          <Text type="secondary">
            只显示卷 <Text code>{volumeFilter}</Text> 的快照
          </Text>
        )}
      </Space>
      {!!error && <ErrorAlert error={error} />}
      {loading && <Spin style={{ display: 'block', margin: '32px auto' }} />}
      <Card>
        {visibleRows?.length ? (
          <ResizableTable
            tableId="snapshots"
            rowKey="snapshot_id"
            size="small"
            tableLayout="fixed"
            columns={columns}
            dataSource={visibleRows}
            pagination={{ pageSize: 10 }}
            scroll={{ x: 'max-content' }}
          />
        ) : (
          <Empty description={pinnedOnly ? '暂无置顶快照' : volumeFilter ? '该卷暂无快照' : '暂无快照'} />
        )}
      </Card>
    </div>
  )
}

function SnapshotOverview({ snapshotId, onDelete }: { snapshotId: string; onDelete: () => void }) {
  const { message } = App.useApp()
  const qc = useQueryClient()
  const { data: snapshot, isLoading: detailLoading, error: detailError } = useSnapshot(snapshotId)
  const { data: stats, isLoading: statsLoading, error: statsError } = useSnapshotStats(snapshotId)
  const [duLimit, setDuLimit] = useState<number>(50)
  const [extLimit, setExtLimit] = useState<number>(10)
  const [topFilesLimit, setTopFilesLimit] = useState<number>(50)
  const { data: du, isLoading: duLoading, error: duError } = useDu(snapshotId, 0, duLimit)

  // 副本校验
  const [verifyReport, setVerifyReport] = useState<VerifyCopyReport | null>(null)
  const handleVerifyResult = (report: VerifyCopyReport) => {
    setVerifyReport(report)
    qc.invalidateQueries({ queryKey: ['snapshot', snapshotId] })
    message.success(report.ok ? '校验完成：副本与源文件一致' : '校验完成：发现异常，请查看结果')
  }

  // 重建统计缓存（任务化）
  const buildStats = useBuildIndexTask()
  const [statsTaskId, setStatsTaskId] = useState<string | null>(null)
  const { task: statsTask } = useTaskEvents(statsTaskId ?? undefined, !!statsTaskId)
  const statsTaskActive = !!statsTask && (statsTask.status === 'pending' || statsTask.status === 'running' || statsTask.status === 'cancelling')

  useEffect(() => {
    if (!statsTask) return
    if (statsTask.status === 'done') {
      message.success('统计缓存重建完成，图表已刷新')
      qc.invalidateQueries({ queryKey: ['snapshot-stats', snapshotId] })
    } else if (statsTask.status === 'error') {
      message.error(`统计缓存重建失败：${statsTask.error ?? '未知错误'}`)
    }
  }, [statsTask, message, qc, snapshotId])

  const submitBuildStats = async () => {
    try {
      const res = await buildStats.mutateAsync({ snapshot_id: snapshotId, kind: 'build_stats' })
      setStatsTaskId(res.task_id)
      message.success(`统计缓存重建任务已提交（${res.task_id.slice(0, 12)}…）`)
    } catch (e) {
      message.warning(apiErrorDetail(e) || '提交失败，请稍后重试')
    }
  }

  // 备注（≤2000 字）
  const patchNotes = usePatchSnapshot()
  const [notesEditing, setNotesEditing] = useState(false)
  const [notesDraft, setNotesDraft] = useState('')

  const saveNotes = async () => {
    try {
      await patchNotes.mutateAsync({ snapshot_id: snapshotId, body: { notes: notesDraft } })
      message.success('备注已保存')
      setNotesEditing(false)
    } catch (e) {
      message.error(apiErrorDetail(e) || '保存失败')
    }
  }

  const error = detailError || statsError || duError

  const extChartOption = useMemo(() => {
    if (!stats?.ext_top_by_count.length) return null
    const items = stats.ext_top_by_count.slice(0, extLimit)
    return {
      tooltip: { trigger: 'axis' },
      grid: { left: 16, right: 16, top: 16, bottom: 8, containLabel: true },
      xAxis: { type: 'value' },
      yAxis: { type: 'category', data: items.map((i) => i.ext || '(无)').reverse() },
      series: [{ type: 'bar', data: items.map((i) => i.count).reverse(), itemStyle: { color: '#aa3bff' } }],
    }
  }, [stats, extLimit])

  const sizeChartOption = useMemo(() => {
    if (!stats?.size_histogram.length) return null
    return {
      tooltip: { trigger: 'axis' },
      grid: { left: 16, right: 16, top: 16, bottom: 24, containLabel: true },
      xAxis: { type: 'category', data: stats.size_histogram.map((i) => i.label), axisLabel: { rotate: 30 } },
      yAxis: { type: 'value' },
      series: [{ type: 'bar', data: stats.size_histogram.map((i) => i.count), itemStyle: { color: '#1677ff' } }],
    }
  }, [stats])

  const depthChartOption = useMemo(() => {
    if (!stats?.depth_histogram.length) return null
    const sorted = [...stats.depth_histogram].sort((a, b) => a.depth - b.depth)
    return {
      tooltip: { trigger: 'axis' },
      grid: { left: 16, right: 16, top: 16, bottom: 24, containLabel: true },
      xAxis: { type: 'category', data: sorted.map((i) => `深度 ${i.depth}`) },
      yAxis: { type: 'value' },
      series: [{ type: 'bar', data: sorted.map((i) => i.count), itemStyle: { color: '#52c41a' } }],
    }
  }, [stats])

  return (
    <div>
      {error && <ErrorAlert error={error} />}

      <Row gutter={[16, 16]}>
        <Col xs={24} sm={12} md={6}>
          <Card>
            {detailLoading ? (
              <Skeleton active paragraph={{ rows: 2 }} />
            ) : (
              <Statistic title="文件总数" value={snapshot?.file_count ?? 0} />
            )}
          </Card>
        </Col>
        <Col xs={24} sm={12} md={6}>
          <Card>
            {detailLoading ? (
              <Skeleton active paragraph={{ rows: 2 }} />
            ) : (
              <Statistic title="目录总数" value={snapshot?.dir_count ?? 0} />
            )}
          </Card>
        </Col>
        <Col xs={24} sm={12} md={6}>
          <Card>
            {detailLoading ? (
              <Skeleton active paragraph={{ rows: 2 }} />
            ) : (
              <Statistic title="总逻辑大小" value={formatFileSize(snapshot?.total_bytes)} />
            )}
          </Card>
        </Col>
        <Col xs={24} sm={12} md={6}>
          <Card>
            {detailLoading ? (
              <Skeleton active paragraph={{ rows: 2 }} />
            ) : (
              <Statistic title="最深层级" value={snapshot?.max_depth ?? 0} suffix="级" />
            )}
          </Card>
        </Col>
      </Row>

      <Row justify="end" align="middle" style={{ marginTop: 8 }}>
        <Space size={12}>
          {statsTaskActive && (
            <Text type="secondary" style={{ fontSize: 12 }}>
              统计缓存重建进行中，完成后自动刷新…
            </Text>
          )}
          <Tooltip title="重建快照库内的预计算统计缓存。仅在数据异常（图表与实际不符）时使用，正常情况无需点击。">
            <span>
              <Button
                size="small"
                icon={<SyncOutlined />}
                loading={buildStats.isPending}
                disabled={statsTaskActive}
                onClick={submitBuildStats}
              >
                重建统计缓存
              </Button>
            </span>
          </Tooltip>
        </Space>
      </Row>

      <Card
        title="元数据"
        style={{ marginTop: 8 }}
        extra={
          <Button danger icon={<DeleteOutlined />} onClick={onDelete}>
            删除快照
          </Button>
        }
      >
        <Space orientation="vertical" style={{ width: '100%' }} size="small">
          <Text>快照 ID：<Text code>{snapshot?.snapshot_id}</Text></Text>
          <Text>卷 ID：<Text code>{snapshot?.volume_id}</Text></Text>
          {snapshot?.volume_nickname && <Text>卷昵称：{snapshot.volume_nickname}</Text>}
          {snapshot?.disk_nickname && <Text>磁盘昵称：{snapshot.disk_nickname}</Text>}
          <Text>采集时间：{formatDateTime(snapshot?.collected_at)}</Text>
          <Text>哈希策略：{snapshot?.hash_policy}</Text>
          <Text>跳过项：{formatNumber(snapshot?.skipped_count)}</Text>
          <div>
            <Text style={{ marginRight: 8 }}>盘上副本：</Text>
            {detailLoading ? <Skeleton.Input size="small" active /> : <OnDiskCopyBadge copy={snapshot?.on_disk_copy ?? null} />}
            {!detailLoading && (
              <VerifyCopyButton
                snapshotId={snapshotId}
                onResult={handleVerifyResult}
              />
            )}
          </div>
          <div>
            <Text style={{ marginRight: 8, verticalAlign: 'top' }}>备注：</Text>
            {notesEditing ? (
              <Space orientation="vertical" style={{ display: 'inline-flex', width: 'calc(100% - 56px)', maxWidth: 560, verticalAlign: 'top' }} size="small">
                <Input.TextArea
                  rows={4}
                  maxLength={2000}
                  showCount
                  value={notesDraft}
                  onChange={(e) => setNotesDraft(e.target.value)}
                  placeholder="记录这块盘的存放位置、备份策略等（最多 2000 字）"
                />
                <Space>
                  <Button type="primary" size="small" loading={patchNotes.isPending} onClick={saveNotes}>
                    保存
                  </Button>
                  <Button size="small" onClick={() => setNotesEditing(false)}>
                    取消
                  </Button>
                </Space>
              </Space>
            ) : (
              <Space size={8} wrap>
                {snapshot?.notes ? (
                  <Text style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{snapshot.notes}</Text>
                ) : (
                  <Text type="secondary">暂无备注</Text>
                )}
                <Button
                  size="small"
                  icon={<EditOutlined />}
                  onClick={() => {
                    setNotesDraft(snapshot?.notes ?? '')
                    setNotesEditing(true)
                  }}
                >
                  {snapshot?.notes ? '编辑备注' : '添加备注'}
                </Button>
              </Space>
            )}
          </div>
        </Space>
      </Card>

      {verifyReport && (
        <VerifyCopyResultCard
          snapshotId={snapshotId}
          report={verifyReport}
          onClose={() => setVerifyReport(null)}
        />
      )}

      <HashPanel snapshotId={snapshotId} />

      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col xs={24} md={12}>
          <Card
            title="扩展名 Top（按数量）"
            extra={
              <Tooltip title="后端统计固定返回前 20 个扩展名">
                <Segmented
                  size="small"
                  value={extLimit}
                  onChange={(v) => setExtLimit(v as number)}
                  options={[
                    { label: '10', value: 10 },
                    { label: '全部（20）', value: 20 },
                  ]}
                />
              </Tooltip>
            }
          >
            {statsLoading ? (
              <Skeleton active paragraph={{ rows: 5 }} />
            ) : extChartOption ? (
              <ReactECharts option={extChartOption} style={{ height: 280 }} />
            ) : (
              <Empty description="暂无数据" />
            )}
          </Card>
        </Col>
        <Col xs={24} md={12}>
          <Card title="文件大小分布">
            {statsLoading ? (
              <Skeleton active paragraph={{ rows: 5 }} />
            ) : sizeChartOption ? (
              <ReactECharts option={sizeChartOption} style={{ height: 280 }} />
            ) : (
              <Empty description="暂无数据" />
            )}
          </Card>
        </Col>
      </Row>

      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col xs={24} md={12}>
          <Card title="深度分布">
            {statsLoading ? (
              <Skeleton active paragraph={{ rows: 5 }} />
            ) : depthChartOption ? (
              <ReactECharts option={depthChartOption} style={{ height: 280 }} />
            ) : (
              <Empty description="暂无数据" />
            )}
          </Card>
        </Col>
        <Col xs={24} md={12}>
          <Card
            title="顶层目录占用 Top"
            extra={
              <Tooltip title="接口单次查询上限 500 条">
                <Segmented
                  size="small"
                  value={duLimit}
                  onChange={(v) => setDuLimit(v as number)}
                  options={[
                    { label: '50', value: 50 },
                    { label: '100', value: 100 },
                    { label: '200', value: 200 },
                    { label: '全部（500）', value: 500 },
                  ]}
                />
              </Tooltip>
            }
          >
            {duLoading ? (
              <Skeleton active />
            ) : du?.items.length ? (
              <Table
                rowKey="entry_id"
                size="small"
                tableLayout="fixed"
                pagination={false}
                scroll={{ y: 280 }}
                columns={[
                  { title: '名称', dataIndex: 'name', ellipsis: true, render: (v: string) => <EllipsisText value={v} /> },
                  { title: '大小', dataIndex: 'total_bytes', render: (v: number) => formatFileSize(v), width: 120 },
                ]}
                dataSource={du.items}
              />
            ) : (
              <Empty description="暂无数据" />
            )}
          </Card>
        </Col>
      </Row>

      <Card
        title="最大文件 Top"
        extra={
          <Tooltip title="后端统计固定返回前 50 个最大文件">
            <Segmented
              size="small"
              value={topFilesLimit}
              onChange={(v) => setTopFilesLimit(v as number)}
              options={[
                { label: '10', value: 10 },
                { label: '25', value: 25 },
                { label: '全部（50）', value: 50 },
              ]}
            />
          </Tooltip>
        }
        style={{ marginTop: 16 }}
      >
        {statsLoading ? (
          <Skeleton active />
        ) : stats?.top_files.length ? (
          <Table
            rowKey="entry_id"
            size="small"
            tableLayout="fixed"
            pagination={false}
            scroll={{ y: 400 }}
            columns={[
              { title: '名称', dataIndex: 'name', ellipsis: true, render: (v: string) => <EllipsisText value={v} /> },
              { title: '路径', dataIndex: 'path', ellipsis: true, render: (v: string) => <EllipsisText value={v} code /> },
              { title: '大小', dataIndex: 'size_bytes', render: (v: number) => formatFileSize(v), width: 120 },
              { title: '修改时间', dataIndex: 'mtime_ns', render: (v: string | null) => nsToDate(v), width: 160 },
            ]}
            dataSource={stats.top_files.slice(0, topFilesLimit)}
          />
        ) : (
          <Empty description="暂无数据" />
        )}
      </Card>
    </div>
  )
}

interface TreeNodeData extends DataNode {
  entryId: number
  isLeaf?: boolean
}

function DirectoryBrowser({ snapshotId }: { snapshotId: string }) {
  const [searchParams, setSearchParams] = useSearchParams()
  const parentIdParam = Number(searchParams.get('parent_id') || '0')
  const [parentId, setParentId] = useState<number>(parentIdParam)
  const [sort, setSort] = useState<SortKey>('name')
  const [order, setOrder] = useState<Order>('asc')
  const [typeFilter, setTypeFilter] = useState<string | undefined>(undefined)
  // 参数级筛选：文本框先草稿、回车/失焦才生效，避免每个键击都打后端
  const [qDraft, setQDraft] = useState('')
  const [qApplied, setQApplied] = useState('')
  const [extDraft, setExtDraft] = useState('')
  const [extApplied, setExtApplied] = useState('')
  const [minSize, setMinSize] = useState<number | undefined>(undefined)
  const [maxSize, setMaxSize] = useState<number | undefined>(undefined)
  // cursor 栈分页：栈底 null = 第一页；上一页弹栈，下一页压入 next_cursor
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE)
  const [cursorStack, setCursorStack] = useState<(string | null)[]>([null])
  const pageIndex = cursorStack.length - 1
  const tableTopRef = useRef<HTMLDivElement | null>(null)
  const mountedRef = useRef(false)
  const [drawerEntry, setDrawerEntry] = useState<Entry | null>(null)
  const [treeData, setTreeData] = useState<TreeNodeData[]>([])
  const [expandedKeys, setExpandedKeys] = useState<React.Key[]>([])
  const rowMenu = useRowContextMenu()

  useEffect(() => {
    setParentId(parentIdParam)
  }, [parentIdParam])

  // 全局搜索「打开所在快照浏览」：沿 open_path 逐级定位目录，并展开目录树沿途节点
  const openPath = searchParams.get('open_path')
  useEffect(() => {
    if (openPath === null) return
    let cancelled = false
    const clean = openPath.replace(/^\/+|\/+$/g, '')
    const finish = (parentIdResolved: number, nodes: DirChainNode[]) => {
      if (cancelled) return
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev)
          next.delete('open_path')
          next.set('parent_id', String(parentIdResolved))
          return next
        },
        { replace: true },
      )
      if (nodes.length) {
        setExpandedKeys((prevKeys) => Array.from(new Set([...prevKeys, ...nodes.map((n) => n.entryId)])))
        setTreeData((prev) => {
          let merged = prev
          for (const node of nodes) merged = updateTreeChildren(merged, node.entryId, node.children)
          return merged
        })
      }
    }
    if (clean === '') {
      finish(0, [])
      return
    }
    void resolveDirChain(snapshotId, clean)
      .then((r) => finish(r.entryId, r.nodes))
      .catch(() => finish(0, []))
    return () => {
      cancelled = true
    }
  }, [openPath, snapshotId, setSearchParams])

  const { data: treeRoot, isLoading: treeLoading } = useTree(snapshotId, 0)
  const { data: entriesRes, isLoading: entriesLoading, error: entriesError } = useEntries(snapshotId, {
    parent_id: parentId,
    limit: pageSize,
    cursor: cursorStack[pageIndex] ?? undefined,
    sort,
    order,
    type: typeFilter as any,
    ext: extApplied || undefined,
    min_size: minSize,
    max_size: maxSize,
    q: qApplied || undefined,
  })
  const entries = entriesRes?.items ?? []

  // 父目录 / 排序 / 筛选变化 → 重置回第一页（渲染期同步，避免 effect 级联）
  const filterKey = `${parentId}\u0000${sort}\u0000${order}\u0000${typeFilter ?? ''}\u0000${extApplied}\u0000${qApplied}\u0000${minSize ?? ''}\u0000${maxSize ?? ''}`
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
    if (entriesRes?.has_more && entriesRes.next_cursor) {
      setCursorStack((s) => [...s, entriesRes.next_cursor!])
    }
  }
  const goPrev = () => setCursorStack((s) => (s.length > 1 ? s.slice(0, -1) : s))
  const changePageSize = (n: number) => {
    setPageSize(n)
    setCursorStack([null])
  }

  const hasFilters = !!(qApplied || extApplied || minSize != null || maxSize != null || typeFilter)

  const applyTextFilters = () => {
    setQApplied(qDraft.trim())
    setExtApplied(extDraft.trim())
  }

  const clearFilters = () => {
    setQDraft('')
    setQApplied('')
    setExtDraft('')
    setExtApplied('')
    setMinSize(undefined)
    setMaxSize(undefined)
    setTypeFilter(undefined)
  }

  useEffect(() => {
    if (treeRoot?.dirs) {
      setTreeData(treeRoot.dirs.map(toTreeNode))
    }
  }, [treeRoot])

  const loadTreeChildren = useCallback(
    async (node: TreeNodeData) => {
      const res = await fetch(`/api/snapshots/${encodeURIComponent(snapshotId)}/tree?parent_id=${node.entryId}`)
      const data = await res.json()
      const children: TreeNodeData[] = data.dirs.map(toTreeNode)
      setTreeData((prev) => updateTreeChildren(prev, node.key, children))
    },
    [snapshotId]
  )

  const onSelectTree = (_: React.Key[], info: { node: TreeNodeData }) => {
    const next = new URLSearchParams(searchParams)
    next.set('parent_id', String(info.node.entryId))
    setSearchParams(next, { replace: true })
  }

  const onTableChange: TableProps<Entry>['onChange'] = (_pagination, _filters, sorter) => {
    const s = Array.isArray(sorter) ? sorter[0] : sorter
    if (!s || !s.columnKey || !s.order) return
    const key = s.columnKey as SortKey
    const nextOrder: Order = s.order === 'descend' ? 'desc' : 'asc'
    if (key === sort && nextOrder === order) return
    setSort(key)
    setOrder(nextOrder)
  }

  const browseTarget = (record: Entry): RowContextTarget => {
    const path = joinChildPath(entriesRes?.parent_path, record.name)
    return {
      path,
      name: record.name,
      snapshotId,
      openDirPath: record.type === 'dir' ? path : dirNameOf(path),
    }
  }

  const columns: TableProps<Entry>['columns'] = [
    {
      title: '名称',
      dataIndex: 'name',
      key: 'name',
      ellipsis: true,
      sorter: true,
      sortOrder: toAntSortOrder(sort === 'name', order),
      render: (_: string, record: Entry) => (
        <Tooltip title={record.name} placement="topLeft" mouseEnterDelay={0.3}>
          <Space style={{ maxWidth: '100%' }}>
            {record.type === 'dir' ? <FolderOutlined /> : <FileOutlined />}
            <Button
              type="link"
              style={{ padding: 0, maxWidth: 480 }}
              onClick={() => {
                if (record.type === 'dir') {
                  const next = new URLSearchParams(searchParams)
                  next.set('parent_id', String(record.entry_id))
                  setSearchParams(next, { replace: true })
                } else {
                  setDrawerEntry(record)
                }
              }}
            >
              <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', display: 'block' }}>
                {record.name}
              </span>
            </Button>
          </Space>
        </Tooltip>
      ),
    },
    {
      title: '类型',
      dataIndex: 'type',
      key: 'type',
      width: 80,
      render: (v: string) => (v === 'dir' ? '目录' : v === 'file' ? '文件' : v),
    },
    {
      title: '大小',
      key: 'size',
      width: 140,
      sorter: true,
      sortOrder: toAntSortOrder(sort === 'size', order),
      render: (_: unknown, record: Entry) => {
        if (record.type === 'dir' && record.rollup) {
          return (
            <Tooltip title={`递归：${formatFileSize(record.rollup.total_bytes)}`}>
              <Text>{formatFileSize(record.rollup.total_bytes)}</Text>
            </Tooltip>
          )
        }
        return formatFileSize(record.size_bytes)
      },
    },
    {
      title: '修改时间',
      key: 'mtime',
      width: 170,
      sorter: true,
      sortOrder: toAntSortOrder(sort === 'mtime', order),
      render: (_: unknown, record: Entry) => nsToDate(record.mtime_ns),
    },
    {
      title: '扩展名',
      dataIndex: 'ext',
      key: 'ext',
      width: 100,
      render: (v: string) => v || '-',
    },
  ]

  return (
    <div>
      {entriesError && <ErrorAlert error={entriesError} />}
      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col xs={24} md={12}>
          <Breadcrumb
            items={[
              {
                title: (
                  <Button
                    type="link"
                    icon={<HomeOutlined />}
                    style={{ padding: 0 }}
                    onClick={() => {
                      const next = new URLSearchParams(searchParams)
                      next.set('parent_id', '0')
                      setSearchParams(next, { replace: true })
                    }}
                  >
                    根目录
                  </Button>
                ),
              },
              { title: `当前：${entriesRes?.parent_path || '.'}` },
            ]}
          />
        </Col>
        <Col xs={24} md={12}>
          <Space wrap>
            <Button icon={<ReloadOutlined />} onClick={() => window.location.reload()}>刷新</Button>
            <Select
              placeholder="类型过滤"
              allowClear
              style={{ width: 120 }}
              value={typeFilter}
              onChange={(v) => setTypeFilter(v)}
            >
              <Option value="file">文件</Option>
              <Option value="dir">目录</Option>
            </Select>
            <Text type="secondary">
              直接子项：文件 {formatNumber(entriesRes?.direct_child_counts.files)} / 目录 {formatNumber(entriesRes?.direct_child_counts.dirs)}
            </Text>
          </Space>
        </Col>
      </Row>

      <Card size="small" style={{ marginBottom: 16 }} styles={{ body: { padding: '10px 16px' } }}>
        <Space wrap>
          <FilterOutlined style={{ color: '#999' }} />
          <Input
            placeholder="名称关键词（回车生效）"
            prefix={<SearchOutlined />}
            allowClear
            style={{ width: 200 }}
            value={qDraft}
            onChange={(e) => setQDraft(e.target.value)}
            onPressEnter={applyTextFilters}
            onBlur={applyTextFilters}
          />
          <Input
            placeholder="扩展名，如 jpg"
            allowClear
            style={{ width: 140 }}
            value={extDraft}
            onChange={(e) => setExtDraft(e.target.value)}
            onPressEnter={applyTextFilters}
            onBlur={applyTextFilters}
          />
          <InputNumber
            placeholder="最小字节"
            min={0}
            style={{ width: 130 }}
            value={minSize}
            onChange={(v) => setMinSize(v ?? undefined)}
          />
          <InputNumber
            placeholder="最大字节"
            min={0}
            style={{ width: 130 }}
            value={maxSize}
            onChange={(v) => setMaxSize(v ?? undefined)}
          />
          <Button type="primary" size="small" onClick={applyTextFilters}>应用筛选</Button>
          <Button size="small" onClick={clearFilters} disabled={!hasFilters}>清除</Button>
          <Text type="secondary" style={{ fontSize: 12 }}>筛选作用于当前目录的直接子项</Text>
        </Space>
      </Card>

      <Row gutter={[16, 16]}>
        <Col xs={24} md={7} lg={5}>
          <Card title="目录树" styles={{ body: { padding: 12, maxHeight: 600, overflow: 'auto' } }}>
            {treeLoading ? (
              <Spin />
            ) : (
              <DirectoryTree
                treeData={treeData}
                loadData={loadTreeChildren as any}
                onSelect={onSelectTree as any}
                expandedKeys={expandedKeys}
                onExpand={(keys) => setExpandedKeys(keys)}
              />
            )}
          </Card>
        </Col>
        <Col xs={24} md={17} lg={19}>
          <Card>
            <div ref={tableTopRef} />
            <ResizableTable
              tableId="browse-entries"
              rowKey="entry_id"
              size="small"
              loading={entriesLoading}
              tableLayout="fixed"
              columns={columns}
              dataSource={entries}
              pagination={false}
              scroll={{ x: 'max-content' }}
              onChange={onTableChange}
              onRow={(record) => ({
                onContextMenu: (e) => rowMenu.open(e, browseTarget(record)),
              })}
              locale={{ emptyText: <Empty description="空目录" /> }}
            />
            <CursorPager
              pageSize={pageSize}
              onPageSizeChange={changePageSize}
              canPrev={pageIndex > 0}
              onPrev={goPrev}
              canNext={entriesRes?.has_more ?? false}
              onNext={goNext}
              hint={`第 ${pageIndex + 1} 页 · 本页 ${entries.length} 条`}
            />
          </Card>
        </Col>
      </Row>
      {rowMenu.element}

      <Drawer
        title={drawerEntry?.name}
        open={!!drawerEntry}
        onClose={() => setDrawerEntry(null)}
        styles={{ wrapper: { width: 480 } }}
      >
        {drawerEntry && (
          <Space orientation="vertical" style={{ width: '100%' }}>
            <Text>类型：{drawerEntry.type}</Text>
            <Text>大小：{formatFileSize(drawerEntry.size_bytes)}</Text>
            <Text>修改时间：{nsToDate(drawerEntry.mtime_ns)}</Text>
            <Text>扩展名：{drawerEntry.ext || '-'}</Text>
            <Text>哈希状态：{drawerEntry.hash_state || '未计算'}</Text>
          </Space>
        )}
      </Drawer>
    </div>
  )
}

function DirectoryTree(props: React.ComponentProps<typeof Tree>) {
  return <Tree.DirectoryTree {...props} />
}

function updateTreeChildren(nodes: TreeNodeData[], key: React.Key, children: TreeNodeData[]): TreeNodeData[] {
  return nodes.map((node) => {
    if (node.key === key) {
      return { ...node, children }
    }
    if (node.children) {
      return { ...node, children: updateTreeChildren(node.children as TreeNodeData[], key, children) }
    }
    return node
  })
}

type SearchMode = 'prefix' | 'fulltext'

const MODE_LABELS: Record<SearchMode, string> = {
  prefix: '前缀',
  fulltext: '全文',
}

/** 探测快照是否已建全文索引（不碰 React 状态）；null = 探测失败。 */
async function fetchFulltextAvailable(snapshotId: string): Promise<boolean | null> {
  try {
    const res = await fetch(`/api/snapshots/${encodeURIComponent(snapshotId)}/search?q=%20&mode=prefix&limit=1`)
    if (!res.ok) return null
    const data = await res.json()
    return typeof data?.fulltext_available === 'boolean' ? data.fulltext_available : null
  } catch {
    return null
  }
}

function SearchPanel({ snapshotId }: { snapshotId: string }) {
  const { message } = App.useApp()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const rowMenu = useRowContextMenu()
  const [q, setQ] = useState('')
  const [mode, setMode] = useState<SearchMode>('prefix')
  const [typeFilter, setTypeFilter] = useState<string | undefined>(undefined)
  const [ext, setExt] = useState('')
  const [minSize, setMinSize] = useState<number | undefined>(undefined)
  const [maxSize, setMaxSize] = useState<number | undefined>(undefined)
  // cursor 栈分页：栈底 null = 第一页；上一页弹栈，下一页压入 next_cursor
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE)
  const [cursorStack, setCursorStack] = useState<(string | null)[]>([null])
  const pageIndex = cursorStack.length - 1
  const tableTopRef = useRef<HTMLDivElement | null>(null)
  const mountedRef = useRef(false)
  const [hasSearched, setHasSearched] = useState(false)
  const [fulltextAvailable, setFulltextAvailable] = useState<boolean | null>(null)

  // 轻量探测当前快照是否已建全文索引，仅用于禁用/提示
  useEffect(() => {
    let cancelled = false
    setFulltextAvailable(null)
    void fetchFulltextAvailable(snapshotId).then((v) => {
      if (!cancelled && v !== null) setFulltextAvailable(v)
    })
    return () => {
      cancelled = true
    }
  }, [snapshotId])

  // 补建全文索引（任务化）：完成后重探测、失效搜索缓存，并在有关键词时自动重试搜索
  const buildFts = useBuildIndexTask()
  const [buildTaskId, setBuildTaskId] = useState<string | null>(null)
  const { task: buildTaskState } = useTaskEvents(buildTaskId ?? undefined, !!buildTaskId)
  const buildTaskActive = !!buildTaskState && (buildTaskState.status === 'pending' || buildTaskState.status === 'running' || buildTaskState.status === 'cancelling')

  useEffect(() => {
    if (!buildTaskState) return
    if (buildTaskState.status === 'done') {
      message.success('全文索引构建完成')
      qc.invalidateQueries({ queryKey: ['search', snapshotId] })
      void fetchFulltextAvailable(snapshotId).then((v) => {
        if (v === null) return
        setFulltextAvailable(v)
        if (v && q.trim().length >= 3) {
          setMode('fulltext')
          setHasSearched(true)
        }
      })
    } else if (buildTaskState.status === 'error') {
      message.error(`全文索引构建失败：${buildTaskState.error ?? '未知错误'}`)
    }
  }, [buildTaskState, message, qc, snapshotId, q])

  const submitBuildFts = async () => {
    try {
      const res = await buildFts.mutateAsync({ snapshot_id: snapshotId, kind: 'build_fts' })
      setBuildTaskId(res.task_id)
      message.success({
        content: (
          <span>
            全文索引构建任务已提交，<Button type="link" size="small" style={{ padding: 0 }} icon={<LinkOutlined />} onClick={() => navigate('/tasks')}>到任务页查看进度</Button>
          </span>
        ),
        duration: 5,
      })
    } catch (e) {
      if (axios.isAxiosError(e)) {
        if (e.response?.status === 409) {
          message.info('该快照已有构建任务在执行中，请等它完成')
        } else {
          message.warning(apiErrorDetail(e) || '提交失败，请稍后重试')
        }
      } else {
        message.error(e instanceof Error ? e.message : String(e))
      }
    }
  }

  // 如果当前选的是全文但快照无索引，自动切回前缀
  useEffect(() => {
    if (mode === 'fulltext' && fulltextAvailable === false) {
      setMode('prefix')
    }
  }, [mode, fulltextAvailable])

  const canSearch = !!q && !(mode === 'fulltext' && q.length < 3)

  const { data: searchRes, isLoading, error, refetch } = useSearch(snapshotId, {
    q,
    mode,
    limit: pageSize,
    cursor: cursorStack[pageIndex] ?? undefined,
    type: typeFilter as any,
    ext: ext || undefined,
    min_size: minSize,
    max_size: maxSize,
  })

  // 关键词 / 模式 / 筛选变化 → 重置回第一页（渲染期同步，避免 effect 级联）
  const filterKey = `${q}\u0000${mode}\u0000${typeFilter ?? ''}\u0000${ext}\u0000${minSize ?? ''}\u0000${maxSize ?? ''}`
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

  const results = searchRes?.items ?? []

  useEffect(() => {
    if (searchRes && typeof searchRes.fulltext_available === 'boolean') {
      setFulltextAvailable(searchRes.fulltext_available)
    }
  }, [searchRes])

  const doSearch = () => {
    setHasSearched(true)
    refetch()
  }

  const goNext = () => {
    if (searchRes?.has_more && searchRes.next_cursor) {
      setCursorStack((s) => [...s, searchRes.next_cursor!])
    }
  }
  const goPrev = () => setCursorStack((s) => (s.length > 1 ? s.slice(0, -1) : s))
  const changePageSize = (n: number) => {
    setPageSize(n)
    setCursorStack([null])
  }

  const columns = [
    { title: '名称', dataIndex: 'name', ellipsis: true, render: (v: string) => <EllipsisText value={v} /> },
    { title: '路径', dataIndex: 'path', ellipsis: true, render: (v: string) => <EllipsisText value={v} code /> },
    { title: '类型', dataIndex: 'type', width: 80, render: (v: string) => (v === 'dir' ? '目录' : v === 'file' ? '文件' : v) },
    { title: '大小', dataIndex: 'size_bytes', width: 120, render: (v: number | null) => formatFileSize(v) },
    { title: '扩展名', dataIndex: 'ext', width: 100, render: (v: string) => v || '-' },
  ]

  // 快照内搜索后端无 sort 参数（按 path 固定排序），不提供表头排序
  const searchTarget = (record: SearchItem): RowContextTarget => ({
    path: record.path,
    name: record.name,
    snapshotId,
    openDirPath: record.type === 'dir' ? record.path : dirNameOf(record.path),
  })

  const placeholder = mode === 'fulltext' ? '输入关键词（至少 3 个字符）…' : '输入关键词前缀…'
  const infoDescription =
    mode === 'fulltext'
      ? '当前为全文搜索模式，命中路径或名称中包含关键词的条目。'
      : '当前为前缀搜索模式，名称以输入关键词开头的条目会被命中。'

  return (
    <div>
      {error && <ErrorAlert error={error} />}
      <Card style={{ marginBottom: 16 }}>
        <Space wrap align="start">
          <Input
            placeholder={placeholder}
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onPressEnter={doSearch}
            prefix={<SearchOutlined />}
            style={{ width: 280 }}
          />
          <Select value={mode} onChange={(v) => setMode(v as SearchMode)} style={{ width: 120 }}>
            <Option value="prefix">{MODE_LABELS.prefix}</Option>
            <Option value="fulltext" disabled={fulltextAvailable === false}>
              {MODE_LABELS.fulltext}
            </Option>
          </Select>
          <Select placeholder="类型" allowClear style={{ width: 120 }} value={typeFilter} onChange={setTypeFilter}>
            <Option value="file">文件</Option>
            <Option value="dir">目录</Option>
          </Select>
          <Input placeholder="扩展名，如 txt" value={ext} onChange={(e) => setExt(e.target.value)} style={{ width: 140 }} />
          <InputNumber placeholder="最小字节" min={0} style={{ width: 120 }} value={minSize} onChange={(v) => setMinSize(v ?? undefined)} />
          <InputNumber placeholder="最大字节" min={0} style={{ width: 120 }} value={maxSize} onChange={(v) => setMaxSize(v ?? undefined)} />
          <Button
            type="primary"
            icon={<SearchOutlined />}
            onClick={doSearch}
            loading={isLoading}
            disabled={!canSearch}
          >
            搜索
          </Button>
        </Space>

        {fulltextAvailable === false && (
          <Alert
            style={{ marginTop: 16, marginBottom: 0 }}
            type="warning"
            showIcon
            title="该快照未构建全文索引，全文搜索不可用"
            description={
              buildTaskActive
                ? '索引构建任务进行中，完成后将自动切换到全文模式并重试搜索。'
                : '可在后台补建索引（构建完成后自动重试搜索）。'
            }
            action={
              <Button
                size="small"
                type="primary"
                ghost
                loading={buildFts.isPending}
                disabled={buildTaskActive}
                onClick={submitBuildFts}
              >
                立即补建全文索引
              </Button>
            }
          />
        )}
        {buildTaskActive && buildTaskState && (
          <Alert
            style={{ marginTop: 8, marginBottom: 0 }}
            type="info"
            showIcon
            title={`全文索引构建中（${buildTaskState.status === 'running' ? '运行中' : '排队中'}）`}
            description={
              <Space orientation="vertical" size="small" style={{ width: '100%' }}>
                <Progress percent={buildTaskState.progress != null ? Math.round(buildTaskState.progress * 100) : 0} size="small" status="active" />
                <Button type="link" size="small" style={{ padding: 0 }} icon={<LinkOutlined />} onClick={() => navigate('/tasks')}>
                  到任务页管理
                </Button>
              </Space>
            }
          />
        )}
        {mode === 'fulltext' && q.length > 0 && q.length < 3 && (
          <Alert
            style={{ marginTop: 16, marginBottom: 0 }}
            type="warning"
            showIcon
            title="全文模式至少 3 个字符"
          />
        )}
      </Card>

      {hasSearched && !isLoading && (
        <Card title={`搜索结果（${formatNumber(results.length)} 条）`}>
          {results.length ? (
            <>
              <div ref={tableTopRef} />
              <ResizableTable
                tableId="snapshot-search"
                rowKey="entry_id"
                size="small"
                tableLayout="fixed"
                columns={columns}
                dataSource={results}
                pagination={false}
                scroll={{ x: 'max-content' }}
                onRow={(record) => ({
                  onContextMenu: (e) => rowMenu.open(e, searchTarget(record)),
                })}
              />
              <CursorPager
                pageSize={pageSize}
                onPageSizeChange={changePageSize}
                canPrev={pageIndex > 0}
                onPrev={goPrev}
                canNext={searchRes?.has_more ?? false}
                onNext={goNext}
                hint={`第 ${pageIndex + 1} 页 · 本页 ${results.length} 条`}
              />
            </>
          ) : (
            <Empty description="无结果，尝试放宽条件" />
          )}
        </Card>
      )}
      {rowMenu.element}

      {!hasSearched && (
        <Alert
          type="info"
          showIcon
          title="输入关键词或选择筛选条件开始搜索"
          description={infoDescription}
        />
      )}
    </div>
  )
}

function DeleteSnapshotModal({
  target,
  onDisk,
  onDiskChange,
  force,
  forceChange,
  blocked,
  error,
  loading,
  onCancel,
  onConfirm,
}: {
  target: { snapshot_id: string; displayName: string; pinned: boolean } | null
  onDisk: boolean
  onDiskChange: (v: boolean) => void
  force: boolean
  forceChange: (v: boolean) => void
  blocked: DeleteSnapshotBlocked | null
  error: string | null
  loading: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  const navigate = useNavigate()
  if (!target) return null

  const hasForceRequirement = !!blocked && (blocked.diffs.length > 0 || blocked.tasks.length > 0)
  const canConfirm = !hasForceRequirement || force

  return (
    <Modal
      title={
        <Space>
          <ExclamationCircleOutlined style={{ color: '#ff4d4f' }} />
          <span>删除快照</span>
        </Space>
      }
      open={!!target}
      onCancel={onCancel}
      onOk={onConfirm}
      confirmLoading={loading}
      okButtonProps={{ danger: true, disabled: !canConfirm }}
      okText="确认删除"
      cancelText="取消"
    >
      <Space orientation="vertical" style={{ width: '100%' }}>
        <Text>即将删除快照：<Text code>{target.displayName}</Text></Text>
        <Text type="secondary">快照 ID：<Text code>{target.snapshot_id}</Text></Text>
        {target.pinned && (
          <Alert
            type="warning"
            showIcon
            title="这是置顶快照"
            description="置顶只是标记，不影响删除；删除后置顶与备注会随快照一并消失。"
          />
        )}
        <Text>该操作会删除主机上的快照目录，且不可恢复。</Text>

        <Checkbox checked={onDisk} onChange={(e) => onDiskChange(e.target.checked)}>
          同时删除盘上副本（<Text code>on_disk=delete</Text>）
        </Checkbox>

        {hasForceRequirement && (
          <Alert
            type="warning"
            showIcon
            title="删除被阻塞"
            description={
              <Space orientation="vertical" size="small" style={{ width: '100%' }}>
                <Text>{blocked!.message}</Text>
                {blocked!.diffs.length > 0 && (
                  <div>
                    <Text type="secondary">关联对比（点击查看）：</Text>
                    <div style={{ marginTop: 4 }}>
                      {blocked!.diffs.map((id) => (
                        <Button
                          key={id}
                          type="link"
                          size="small"
                          style={{ padding: 0, marginRight: 12 }}
                          icon={<LinkOutlined />}
                          onClick={() => navigate(`/diff?id=${encodeURIComponent(id)}`)}
                        >
                          <Text code>{id}</Text>
                        </Button>
                      ))}
                    </div>
                  </div>
                )}
                {blocked!.tasks.length > 0 && (
                  <div>
                    <Text type="secondary">活跃任务：</Text>
                    {blocked!.tasks.map((id) => (
                      <Text key={id} code style={{ marginRight: 8 }}>{id}</Text>
                    ))}
                    <Button type="link" size="small" style={{ padding: 0 }} onClick={() => navigate('/tasks')}>
                      到任务页查看
                    </Button>
                  </div>
                )}
                <Checkbox checked={force} onChange={(e) => forceChange(e.target.checked)}>
                  我已确认，同时删除相关对比（<Text code>force=true</Text>）
                </Checkbox>
              </Space>
            }
          />
        )}

        {error && !hasForceRequirement && <Alert type="error" showIcon title={error} />}
      </Space>
    </Modal>
  )
}

function ExportPanel({ snapshotId }: { snapshotId: string }) {
  const [sections, setSections] = useState<string[]>(REPORT_SECTIONS.map((s) => s.value))

  const allSelected = sections.length === REPORT_SECTIONS.length
  const reportUrl = snapshotReportUrl(snapshotId, sections)
  const reportFileName = `cldm_${snapshotId.replace(/\//g, '_')}_report.html`

  return (
    <Card title="导出快照数据">
      <Space orientation="vertical" style={{ width: '100%' }} size="middle">
        <Alert
          type="info"
          showIcon
          title="导出格式说明"
          description="CSV：entries 全表；V1 CSV：兼容旧版的三件套 zip；HTML 报告：自包含的单文件网页，含统计图表与跳过项汇总。"
        />

        <Card
          type="inner"
          title="HTML 报告分节"
          extra={
            <Button
              type="link"
              size="small"
              onClick={() => setSections(allSelected ? [] : REPORT_SECTIONS.map((s) => s.value))}
            >
              {allSelected ? '全不选' : '全选'}
            </Button>
          }
        >
          <Space orientation="vertical" size="middle" style={{ width: '100%' }}>
            <Checkbox.Group
              options={REPORT_SECTIONS.map((s) => ({ label: s.label, value: s.value }))}
              value={sections}
              onChange={(v) => setSections(v as string[])}
            />
            <Space wrap>
              <Button
                type="primary"
                icon={<FileTextOutlined />}
                href={reportUrl}
                download={reportFileName}
                disabled={sections.length === 0}
              >
                生成 HTML 报告（{sections.length}/{REPORT_SECTIONS.length} 节）
              </Button>
              {sections.length === 0 && (
                <Text type="secondary">至少勾选一个分节才能生成报告</Text>
              )}
            </Space>
          </Space>
        </Card>

        <Space wrap>
          <Button icon={<DownloadOutlined />} href={exportSnapshotUrl(snapshotId, 'csv')}>
            导出 CSV
          </Button>
          <Button icon={<DownloadOutlined />} href={exportSnapshotUrl(snapshotId, 'v1_csv')}>
            导出 V1 CSV（zip）
          </Button>
        </Space>
      </Space>
    </Card>
  )
}
