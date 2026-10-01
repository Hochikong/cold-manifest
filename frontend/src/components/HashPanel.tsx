import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Alert,
  App,
  Button,
  Card,
  Collapse,
  Input,
  Progress,
  Select,
  Space,
  Statistic,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import {
  FieldTimeOutlined,
  LinkOutlined,
  StopOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import { useQueryClient } from '@tanstack/react-query'
import { useHashSummary, useSnapshot, useSubmitHash, useTaskEvents } from '../api/hooks'
import { formatNumber } from '../utils/format'
import { apiErrorDetail, type HashTaskBody, type HashTaskResult } from '../api/client'

const { Text } = Typography

const POLICY_LABEL: Record<string, string> = {
  none: '未启用',
  full: '完整哈希',
  sampled: '抽样指纹',
}

const SCOPE_LABEL: Record<string, string> = {
  incremental: '增量（未算过的文件）',
  candidates: '只算重复候选（快）',
  group: '单组精验',
}

/**
 * 指纹 / 哈希面板（快照概览）：
 * - 展示当前哈希策略（meta 优先，回退 catalog 行）与已哈希文件数；
 * - 快捷提交「候选指纹」（sampled + candidates，秒级~分钟级）与
 *   「完整哈希」（full + incremental，代价高，附说明）；
 * - 高级选项可自选算法 / 策略 / 范围，或输入 hash 前缀对单组精验；
 * - 提交后内联跟踪任务进度，任务结束后自动刷新策略与计数。
 */
export default function HashPanel({ snapshotId }: { snapshotId: string }) {
  const { message, notification } = App.useApp()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const submitHash = useSubmitHash()
  // 最近一次提交的参数：任务结果里 scope 缺失时用来解释「为什么什么都没算」
  const lastBodyRef = useRef<HashTaskBody | null>(null)

  const { data: snapshot } = useSnapshot(snapshotId)
  const meta = snapshot?.meta ?? {}
  const currentPolicy = meta.hash_policy || snapshot?.hash_policy || 'none'
  // policy=none 时不发指纹探测请求：该请求必然 400，会在浏览器控制台留一条报错
  const { data: summary, isLoading: summaryLoading, error: summaryError } = useHashSummary(
    snapshotId,
    currentPolicy !== 'none',
  )

  const [taskId, setTaskId] = useState<string | null>(null)
  const { task } = useTaskEvents(taskId ?? undefined, !!taskId)
  // 注：任务完成后不清 taskId（避免 effect 内同步 setState）；再次提交会直接覆盖

  // 高级选项
  const [algo, setAlgo] = useState<HashTaskBody['algo']>('sha256')
  const [policy, setPolicy] = useState<HashTaskBody['policy']>('sampled')
  const [scope, setScope] = useState<NonNullable<HashTaskBody['scope']>>('candidates')
  const [group, setGroup] = useState('')

  const hashedFiles = currentPolicy === 'none' ? 0 : summary ? summary.hashed_files : summaryError ? 0 : undefined
  const totalFiles = snapshot?.file_count ?? 0
  const hashPercent = hashedFiles != null && totalFiles > 0 ? Math.min(100, (hashedFiles / totalFiles) * 100) : 0

  const taskActive = !!task && (task.status === 'pending' || task.status === 'running' || task.status === 'cancelling')

  useEffect(() => {
    if (!task) return
    if (task.status === 'done') {
      // 用后端 result 里的真实数字说话；字段缺失（旧后端）时退化为通用提示
      const r = (task.result ?? {}) as HashTaskResult
      const total = typeof r.total === 'number' ? r.total : null
      const computed = typeof r.computed === 'number' ? r.computed : 0
      const cached = typeof r.cached === 'number' ? r.cached : 0
      const errors = typeof r.errors === 'number' ? r.errors : 0
      const elapsed = typeof r.elapsed_s === 'number' ? r.elapsed_s : null
      const scope = r.scope ?? lastBodyRef.current?.scope
      if (total === 0) {
        message.info({
          content:
            scope === 'candidates'
              ? '没有需要计算的文件：快照里没有大小相同的候选文件，本次无需计算指纹'
              : '没有需要计算的文件：范围内所有文件都已算过',
          duration: 6,
        })
      } else if (total != null && computed === 0 && cached === 0 && errors === 0) {
        message.info({
          content: `哈希任务完成，但没有任何文件被处理（范围 ${formatNumber(total)} 个），统计未变化`,
          duration: 6,
        })
      } else {
        notification.success({
          title: '哈希任务完成',
          description: (
            <Space orientation="vertical" size={0}>
              <Text>
                范围 {total != null ? formatNumber(total) : '—'} 个文件：本次计算{' '}
                {formatNumber(computed)} 个
                {cached > 0 ? `，缓存命中 ${formatNumber(cached)} 个` : ''}
                {errors > 0 && <Text type="danger">，失败 {formatNumber(errors)} 个</Text>}
              </Text>
              {elapsed != null && <Text type="secondary">耗时 {elapsed.toFixed(1)} 秒</Text>}
              {errors > 0 && (
                <Text type="secondary">失败的文件已标记，下次续算时自动跳过</Text>
              )}
            </Space>
          ),
          placement: 'top',
          duration: 8,
        })
      }
      qc.invalidateQueries({ queryKey: ['snapshot', snapshotId] })
      qc.invalidateQueries({ queryKey: ['hash-summary', snapshotId] })
      qc.invalidateQueries({ queryKey: ['duplicates', snapshotId] })
      qc.invalidateQueries({ queryKey: ['snapshots'] })
    } else if (task.status === 'error') {
      message.error(`哈希任务失败：${task.error ?? '未知错误'}`)
    } else if (task.status === 'cancelled') {
      message.info('哈希任务已取消（已算部分保留）')
      qc.invalidateQueries({ queryKey: ['hash-summary', snapshotId] })
    }
  }, [task, message, notification, qc, snapshotId])

  const submit = async (body: HashTaskBody) => {
    lastBodyRef.current = body
    try {
      const res = await submitHash.mutateAsync({ snapshot_id: snapshotId, body })
      setTaskId(res.task_id)
      message.success({
        content: `哈希任务已提交（${res.task_id.slice(0, 12)}…）`,
        duration: 4,
      })
    } catch (e) {
      message.warning(apiErrorDetail(e) || '提交失败，请稍后重试')
    }
  }

  const submitQuick = (body: HashTaskBody) => {
    if (taskActive) {
      message.warning('已有进行中的哈希任务，请等它完成或取消')
      return
    }
    void submit(body)
  }

  const submitAdvanced = () => {
    const trimmedGroup = group.trim()
    if (trimmedGroup) {
      // 单组精验：固定 full + incremental，group 与 candidates 互斥
      submitQuick({ algo, policy: 'full', scope: 'incremental', group: trimmedGroup })
      return
    }
    submitQuick({ algo, policy, scope })
  }

  return (
    <Card
      title="指纹 / 哈希"
      style={{ marginTop: 16 }}
      extra={
        taskId && (
          <Button type="link" size="small" icon={<LinkOutlined />} onClick={() => navigate('/tasks')}>
            去任务页
          </Button>
        )
      }
    >
      <Space orientation="vertical" style={{ width: '100%' }} size="middle">
        <Space wrap size="large" align="start">
          <Statistic
            title="当前哈希策略"
            value={POLICY_LABEL[currentPolicy] ?? currentPolicy}
            suffix={
              meta.hash_scope && (
                <Tooltip title={`范围：${SCOPE_LABEL[meta.hash_scope] ?? meta.hash_scope}${meta.hash_algo ? ` · ${meta.hash_algo}` : ''}`}>
                  <Tag style={{ marginLeft: 8 }}>{meta.hash_scope}</Tag>
                </Tooltip>
              )
            }
          />
          <Statistic
            title="已哈希文件数"
            value={summaryLoading ? '…' : formatNumber(hashedFiles ?? 0)}
            suffix={totalFiles > 0 ? <Text type="secondary" style={{ fontSize: 14 }}>/ {formatNumber(totalFiles)}</Text> : undefined}
          />
        </Space>

        {hashPercent > 0 && (
          <Progress
            percent={Number(hashPercent.toFixed(1))}
            size="small"
            status={hashPercent >= 100 ? 'success' : 'normal'}
            format={(p) => `${p}%`}
          />
        )}

        {(summaryError || currentPolicy === 'none') && (
          <Text type="secondary" style={{ fontSize: 12 }}>
            该快照还没有任何可用哈希（完整或抽样指纹）。
          </Text>
        )}

        <Space wrap>
          <Tooltip title="只对「大小重复」的候选文件算指纹（首/中/尾 64KB 抽样），通常秒级到分钟级，无需整盘读取。">
            <Button
              type="primary"
              icon={<ThunderboltOutlined />}
              loading={submitHash.isPending}
              disabled={taskActive}
              onClick={() => submitQuick({ algo: 'sha256', policy: 'sampled', scope: 'candidates' })}
            >
              算指纹（只算候选 · 快）
            </Button>
          </Tooltip>
          <Tooltip title="逐字节读取源文件计算 SHA-256；USB 机械盘读满 1TB 约需 2–3 小时。">
            <Button
              icon={<FieldTimeOutlined />}
              loading={submitHash.isPending}
              disabled={taskActive}
              onClick={() =>
                submitQuick({ algo: 'sha256', policy: 'full', scope: 'incremental' })
              }
            >
              算完整哈希（慢）
            </Button>
          </Tooltip>
        </Space>

        <Alert
          type="warning"
          showIcon
          title="完整哈希代价说明"
          description="完整哈希需要逐字节读取源盘上的每个文件：USB 机械盘读满 1TB 约需 2–3 小时，且期间盘不能断开。只是查重的话，先「算指纹」通常就够用了。"
        />

        {taskActive && task && (
          <Alert
            type="info"
            showIcon
            title={`哈希任务进行中（${task.status === 'cancelling' ? '正在取消' : task.status === 'running' ? '运行中' : '排队中'}）`}
            description={
              <Space orientation="vertical" size="small" style={{ width: '100%' }}>
                <Progress
                  percent={task.progress != null ? Math.round(task.progress * 100) : 0}
                  status="active"
                  size="small"
                />
                <Text type="secondary">{task.message ?? '正在计算…'}</Text>
                <Button size="small" icon={<StopOutlined />} onClick={() => navigate('/tasks')}>
                  到任务页管理
                </Button>
              </Space>
            }
          />
        )}

        {task?.status === 'error' && (
          <Alert
            type="error"
            showIcon
            title="哈希任务失败"
            description={
              <Space orientation="vertical" size={0}>
                <Text>{task.error ?? '未知错误'}</Text>
                <Text type="secondary">
                  常见原因：源盘未挂载或路径已变（「源目录不可用」）、快照库被其他任务占用。确认源盘接入后可重新提交。
                </Text>
              </Space>
            }
          />
        )}

        <Collapse
          ghost
          items={[
            {
              key: 'advanced',
              label: '高级选项（自选算法 / 策略 / 范围，或单组精验）',
              children: (
                <Space orientation="vertical" size="small" style={{ width: '100%' }}>
                  <Space wrap>
                    <Select
                      value={algo}
                      onChange={setAlgo}
                      style={{ width: 140 }}
                      options={[{ value: 'sha256', label: '算法：sha256' }]}
                    />
                    <Select
                      value={policy}
                      onChange={setPolicy}
                      style={{ width: 160 }}
                      options={[
                        { value: 'sampled', label: '策略：抽样指纹' },
                        { value: 'full', label: '策略：完整哈希' },
                      ]}
                    />
                    <Select
                      value={scope}
                      onChange={setScope}
                      style={{ width: 220 }}
                      disabled={!!group.trim()}
                      options={[
                        { value: 'candidates', label: '范围：只算重复候选' },
                        { value: 'incremental', label: '范围：增量（全部未算）' },
                      ]}
                    />
                  </Space>
                  <Space.Compact style={{ width: '100%' }}>
                    <Input
                      placeholder="单组精验：输入 hash 前缀（如 3f2a…），留空则按上方选项提交"
                      value={group}
                      onChange={(e) => setGroup(e.target.value)}
                      onPressEnter={submitAdvanced}
                      allowClear
                    />
                    <Button type="primary" ghost loading={submitHash.isPending} disabled={taskActive} onClick={submitAdvanced}>
                      提交
                    </Button>
                  </Space.Compact>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    单组精验会强制用完整哈希重算该组所有文件（与「只算候选」互斥）；适合复核某个指纹重复组的真实内容。
                  </Text>
                </Space>
              ),
            },
          ]}
        />
      </Space>
    </Card>
  )
}
