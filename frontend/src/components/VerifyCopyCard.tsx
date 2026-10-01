import { useState } from 'react'
import {
  Alert,
  App,
  Button,
  Card,
  Descriptions,
  Modal,
  Radio,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd'
import { CloseOutlined, SafetyCertificateOutlined } from '@ant-design/icons'
import { useQueryClient } from '@tanstack/react-query'
import axios from 'axios'
import { useVerifyCopy } from '../api/hooks'
import type { VerifyCopyReport, VerifyCopySample } from '../api/client'

const { Text } = Typography

const COPY_STATUS_META: Record<string, { color: string; label: string }> = {
  ok: { color: 'success', label: '副本一致' },
  mismatch: { color: 'error', label: '副本不一致' },
  missing_disk: { color: 'error', label: '盘上副本缺失' },
  not_recorded: { color: 'default', label: '无登记（未开启副本或旧导入）' },
}

const SAMPLE_STATUS_META: Record<string, { color: string; label: string }> = {
  mismatch: { color: 'error', label: '不一致' },
  missing: { color: 'warning', label: '文件缺失' },
  unreadable: { color: 'warning', label: '不可读' },
}

type VerifyScope = 'sample' | 'full'

function axiosDetail(e: unknown): string {
  if (axios.isAxiosError(e)) {
    const detail = e.response?.data
    if (typeof detail === 'string') return detail
    if (detail && typeof detail === 'object' && 'detail' in detail) {
      const d = (detail as { detail?: unknown }).detail
      if (typeof d === 'string') return d
    }
  }
  return e instanceof Error ? e.message : String(e)
}

/**
 * 「校验盘上副本」按钮 + 范围选择弹窗：抽样 200（快）/ 全量（慢，附耗时提示）。
 * 校验为同步接口，完成后把报告交给 onResult（由概览渲染结果卡片并刷新徽标）。
 */
export function VerifyCopyButton({
  snapshotId,
  onResult,
}: {
  snapshotId: string
  onResult: (report: VerifyCopyReport) => void
}) {
  const verify = useVerifyCopy()
  const [open, setOpen] = useState(false)
  const [scope, setScope] = useState<VerifyScope>('sample')
  const [error, setError] = useState<string | null>(null)

  const run = async () => {
    setError(null)
    try {
      const report = await verify.mutateAsync({
        snapshot_id: snapshotId,
        body: scope === 'full' ? { scope: 'full' } : { scope: 'sample', sample_size: 200 },
      })
      setOpen(false)
      onResult(report)
    } catch (e) {
      setError(axiosDetail(e))
    }
  }

  return (
    <>
      <Button
        size="small"
        icon={<SafetyCertificateOutlined />}
        loading={verify.isPending}
        onClick={() => {
          setError(null)
          setOpen(true)
        }}
      >
        校验盘上副本
      </Button>
      <Modal
        title="校验盘上副本"
        open={open}
        onCancel={() => (verify.isPending ? undefined : setOpen(false))}
        mask={{ closable: !verify.isPending }}
        cancelButtonProps={{ disabled: verify.isPending }}
        onOk={run}
        okText="开始校验"
        okButtonProps={{ loading: verify.isPending }}
      >
        <Space orientation="vertical" size="middle" style={{ width: '100%' }}>
          <Radio.Group
            value={scope}
            onChange={(e) => setScope(e.target.value as VerifyScope)}
            disabled={verify.isPending}
          >
            <Space orientation="vertical" size="small">
              <Radio value="sample">
                抽样校验（随机抽 200 个文件回源重算哈希，通常数秒）
              </Radio>
              <Radio value="full">
                全量校验（逐文件重算全部哈希；USB 机械盘读约 100MB/s，大快照可能耗时数小时）
              </Radio>
            </Space>
          </Radio.Group>
          <Text type="secondary" style={{ fontSize: 12 }}>
            副本对账总是执行；源文件抽检仅对完整哈希（hash_policy=full）的快照可用。
          </Text>
          {error && <Alert type="error" showIcon title="校验无法进行" description={error} />}
        </Space>
      </Modal>
    </>
  )
}

function shortSha(sha: string | null): string {
  return sha ? sha.slice(0, 12) : '-'
}

/** 副本校验结果卡片：状态徽标 + 抽检计数 + 异常路径明细（最多列 10 条）。 */
export function VerifyCopyResultCard({
  snapshotId,
  report,
  onClose,
}: {
  snapshotId: string
  report: VerifyCopyReport
  onClose: () => void
}) {
  const qc = useQueryClient()
  const { message } = App.useApp()
  const copyMeta = COPY_STATUS_META[report.copy.status] ?? { color: 'default', label: report.copy.status }
  const source = report.source
  const abnormal: VerifyCopySample[] = source.samples.filter((s) => s.status !== 'match')

  const refreshBadge = () => {
    qc.invalidateQueries({ queryKey: ['snapshot', snapshotId] })
    message.success('已刷新盘上副本状态')
  }

  return (
    <Card
      title={
        <Space>
          <span>副本校验结果</span>
          <Tag color={report.ok ? 'success' : 'error'}>{report.ok ? '校验通过' : '发现异常'}</Tag>
          <Tag>{copyMeta.label}</Tag>
        </Space>
      }
      extra={
        <Space>
          <Button size="small" onClick={refreshBadge}>刷新徽标</Button>
          <Button size="small" type="text" icon={<CloseOutlined />} onClick={onClose} aria-label="关闭校验结果" />
        </Space>
      }
      style={{ marginTop: 16 }}
    >
      <Descriptions
        size="small"
        column={1}
        items={[
          {
            key: 'copy',
            label: '副本对账',
            children: (
              <Space size={8} wrap>
                <Tag color={copyMeta.color}>{copyMeta.label}</Tag>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  登记值 {shortSha(report.copy.recorded_sha256)} · 盘上现值 {shortSha(report.copy.disk_sha256)}
                </Text>
              </Space>
            ),
          },
          {
            key: 'host',
            label: '主机快照库',
            children: report.copy.host_status === 'unchanged' ? (
              <Text type="secondary">自采集以来未变动</Text>
            ) : (
              <Text type="secondary">采集后有改动（哈希 / 索引重建等会更新主机库，属正常现象）</Text>
            ),
          },
          {
            key: 'sidecar',
            label: '旁车文件',
            children: report.sidecar.status === 'ok' ? (
              <Text type="secondary">snapshot.json 与快照库一致</Text>
            ) : (
              <Space orientation="vertical" size={2}>
                <Tag color="warning">{report.sidecar.status === 'missing' ? '缺失' : '存在问题'}</Tag>
                {report.sidecar.problems.map((p) => (
                  <Text key={p} type="warning" style={{ fontSize: 12 }}>{p}</Text>
                ))}
              </Space>
            ),
          },
          {
            key: 'source',
            label: '源文件抽检',
            children: source.available ? (
              <Space size={8} wrap>
                <Text>
                  共抽检 <Text strong>{source.checked}</Text> 个文件
                </Text>
                <Text type="success">一致 {source.match}</Text>
                {source.mismatch > 0 && <Text type="danger">不一致 {source.mismatch}</Text>}
                {source.missing > 0 && <Text type="warning">缺失 {source.missing}</Text>}
                {source.unreadable > 0 && <Text type="warning">不可读 {source.unreadable}</Text>}
              </Space>
            ) : (
              <Text type="secondary">{source.error ?? '源文件抽检不可用'}</Text>
            ),
          },
        ]}
      />

      {abnormal.length > 0 && (
        <>
          <Text strong style={{ display: 'block', margin: '12px 0 8px' }}>
            异常明细（前 {Math.min(abnormal.length, 10)} 条，共 {abnormal.length} 条）
          </Text>
          <Table
            rowKey="path"
            size="small"
            pagination={false}
            dataSource={abnormal.slice(0, 10)}
            columns={[
              { title: '路径', dataIndex: 'path', ellipsis: true },
              {
                title: '状态',
                dataIndex: 'status',
                width: 120,
                render: (v: string) => {
                  const meta = SAMPLE_STATUS_META[v] ?? { color: 'default', label: v }
                  return <Tag color={meta.color}>{meta.label}</Tag>
                },
              },
            ]}
          />
        </>
      )}
    </Card>
  )
}
