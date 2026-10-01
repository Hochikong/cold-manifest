import { useState } from 'react'
import axios from 'axios'
import {
  Alert,
  App,
  Button,
  Card,
  Col,
  Collapse,
  Row,
  Space,
  Statistic,
  Typography,
} from 'antd'
import { ExperimentOutlined, BuildOutlined, QuestionCircleOutlined } from '@ant-design/icons'
import { useRebuildCatalog } from '../api/hooks'
import { apiErrorDetail, type RebuildCatalogSummary } from '../api/client'

const { Text } = Typography

/** 预演/执行结果里的计数行（按展示顺序）。 */
const COUNTER_ITEMS: { key: keyof RebuildCatalogSummary; label: string }[] = [
  { key: 'scanned', label: '扫描到有效快照库' },
  { key: 'disks_added', label: '补建磁盘' },
  { key: 'volumes_added', label: '补建卷' },
  { key: 'snapshots_added', label: '补建快照' },
  { key: 'copies_added', label: '补建盘上副本' },
  { key: 'fields_backfilled', label: '回填空缺字段' },
  { key: 'snapshots_skipped', label: '已登记无变化' },
]

function counterValue(summary: RebuildCatalogSummary, key: keyof RebuildCatalogSummary): number {
  const v = summary[key]
  return typeof v === 'number' ? v : 0
}

function SummaryGrid({ summary }: { summary: RebuildCatalogSummary }) {
  return (
    <div>
      <Row gutter={[12, 12]}>
        {COUNTER_ITEMS.map(({ key, label }) => (
          <Col xs={12} sm={8} md={6} key={key}>
            <Statistic title={label} value={counterValue(summary, key)} />
          </Col>
        ))}
      </Row>
      {summary.warnings.length > 0 && (
        <Collapse
          ghost
          size="small"
          style={{ marginTop: 12 }}
          items={[
            {
              key: 'warnings',
              label: `警告明细（${summary.warnings.length}）`,
              children: (
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {summary.warnings.map((w, i) => (
                    <li key={i}>
                      <Text type="secondary" style={{ fontSize: 12 }}>{w}</Text>
                    </li>
                  ))}
                </ul>
              ),
            },
          ]}
        />
      )}
    </div>
  )
}

/** 设置页「数据修复」卡片：catalog 丢失/损坏时的重建登记（预演 → 确认 → 执行）。 */
export default function RebuildCatalogCard() {
  const { message, modal } = App.useApp()
  const rebuild = useRebuildCatalog()
  const [dryRunResult, setDryRunResult] = useState<RebuildCatalogSummary | null>(null)
  const [executedResult, setExecutedResult] = useState<RebuildCatalogSummary | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [executing, setExecuting] = useState(false)

  const showError = (e: unknown) => {
    if (axios.isAxiosError(e) && e.response?.status === 409) {
      message.error('数据根被占用（正在采集/哈希），稍后再试')
      return
    }
    const detail = apiErrorDetail(e)
    message.error(detail)
    setError(detail)
  }

  const runDry = async () => {
    setError(null)
    setExecutedResult(null)
    try {
      const res = await rebuild.mutateAsync(true)
      setDryRunResult(res)
      message.success(`预演完成：扫描 ${res.scanned} 个快照库，将补建 ${res.snapshots_added} 个快照登记`)
    } catch (e) {
      setDryRunResult(null)
      showError(e)
    }
  }

  const confirmExecute = () => {
    modal.confirm({
      title: '执行 catalog 重建？',
      icon: <QuestionCircleOutlined />,
      content: (
        <Space orientation="vertical" size={4}>
          <Text>将扫描数据根下所有 snapshot.db 的元数据，重建/回填 catalog 的磁盘、卷、快照与副本登记行。</Text>
          <Text strong>不会修改任何快照库本身；已有登记值不会被覆盖（只回填空缺字段）。</Text>
          <Text type="secondary">注意：采集后用 cldm hash 改过哈希策略的快照，重建后会回到元数据里记录的策略。</Text>
        </Space>
      ),
      okText: '执行重建',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        setError(null)
        setExecuting(true)
        try {
          const res = await rebuild.mutateAsync(false)
          setExecutedResult(res)
          message.success(
            `重建完成：新增 ${res.snapshots_added} 个快照、${res.disks_added} 个磁盘、${res.volumes_added} 个卷登记，回填 ${res.fields_backfilled} 个字段`
          )
        } catch (e) {
          showError(e)
        } finally {
          setExecuting(false)
        }
      },
    })
  }

  return (
    <Card title="数据修复" style={{ marginTop: 16 }}>
      <Space orientation="vertical" style={{ width: '100%' }} size="middle">
        <Text type="secondary">
          catalog 登记丢失或损坏时，扫描数据根下各快照库的元数据重建登记（补建磁盘/卷/快照/副本行，只回填空缺字段，
          <Text strong>不会改快照库本身</Text>）。建议先预演确认计划，再执行重建。
        </Text>

        <Space wrap>
          <Button icon={<ExperimentOutlined />} loading={rebuild.isPending && !executing} onClick={runDry}>
            预演（dry-run）
          </Button>
          {dryRunResult && (
            <Button type="primary" danger icon={<BuildOutlined />} loading={executing} onClick={confirmExecute}>
              执行重建
            </Button>
          )}
        </Space>

        {error && (
          <Alert type="error" showIcon title="重建失败" description={error} closable onClose={() => setError(null)} />
        )}

        {dryRunResult && (
          <div>
            <Text strong>预演结果（未写入任何数据）</Text>
            {dryRunResult.snapshots_added === 0 &&
              dryRunResult.disks_added === 0 &&
              dryRunResult.volumes_added === 0 &&
              dryRunResult.copies_added === 0 &&
              dryRunResult.fields_backfilled === 0 &&
              dryRunResult.warnings.length === 0 && (
                <Text type="secondary" style={{ marginLeft: 8 }}>
                  所有快照库均已登记且字段完整，无需修复。
                </Text>
              )}
            <div style={{ marginTop: 8 }}>
              <SummaryGrid summary={dryRunResult} />
            </div>
          </div>
        )}

        {executedResult && (
          <Alert type="success" showIcon title="已执行重建" description={<SummaryGrid summary={executedResult} />} />
        )}
      </Space>
    </Card>
  )
}
