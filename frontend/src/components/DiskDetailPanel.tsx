import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Alert, Button, Card, Collapse, Descriptions, Empty, Row, Col, Skeleton, Space, Table, Tag, Tooltip, Typography } from 'antd'
import { CheckCircleOutlined, ExclamationCircleOutlined, MedicineBoxOutlined, ThunderboltOutlined } from '@ant-design/icons'
import { useDisk, useDiskSmartRead } from '../api/hooks'
import ErrorAlert from './ErrorAlert'
import SmartTrendsChart from './SmartTrendsChart'
import VolumeTrendsChart from './VolumeTrendsChart'
import VolumeDetailDrawer from './VolumeDetailDrawer'
import { apiErrorDetail, parseSmartIdentity, type DiskDetail, type DiskVolume, type DiskSnapshot, type ParsedSmart, type SmartReadResult } from '../api/client'
import { formatDateTime, formatFileSize, formatNumber } from '../utils/format'

const { Text } = Typography

interface DiskDetailPanelProps {
  disk_id: string
}

export default function DiskDetailPanel({ disk_id }: DiskDetailPanelProps) {
  const navigate = useNavigate()
  const { data: disk, isLoading, error } = useDisk(disk_id)
  const smartRead = useDiskSmartRead()
  const [readResult, setReadResult] = useState<SmartReadResult | null>(null)
  const [readError, setReadError] = useState<string | null>(null)

  const handleReadNow = async () => {
    setReadResult(null)
    setReadError(null)
    try {
      const res = await smartRead.mutateAsync(disk_id)
      setReadResult(res)
    } catch (e) {
      // 404 = 盘当前不在线；其他为网络/服务错误。都给人话原因，不弹 toast。
      setReadError(apiErrorDetail(e))
    }
  }

  if (error) {
    return <ErrorAlert error={error} />
  }

  if (isLoading || !disk) {
    return (
      <Space orientation="vertical" style={{ width: '100%' }} size="middle">
        <Skeleton active paragraph={{ rows: 4 }} />
        <Skeleton active paragraph={{ rows: 6 }} />
      </Space>
    )
  }

  const liveParsed = readResult?.ok ? readResult.parsed : null
  const liveLabel = readResult?.ok ? '现场读取 · smartctl' : null

  return (
    <Space orientation="vertical" style={{ width: '100%' }} size="middle">
      <Row gutter={[16, 16]}>
        <Col xs={24} lg={12}>
          <IdentityCard disk={disk} liveParsed={liveParsed} liveLabel={liveLabel} />
        </Col>
        <Col xs={24} lg={12}>
          <HealthCard
            disk={disk}
            readResult={readResult}
            readError={readError}
            reading={smartRead.isPending}
            onReadNow={handleReadNow}
          />
        </Col>
      </Row>

      <SmartTrendsChart disk_id={disk_id} />

      <Row gutter={[16, 16]}>
        <Col xs={24} lg={12}>
          <VolumesCard volumes={disk.volumes} />
        </Col>
        <Col xs={24} lg={12}>
          <SnapshotsCard snapshots={disk.snapshots} onSelect={(id) => navigate(`/snapshots?snapshot=${encodeURIComponent(id)}&tab=overview`)} />
        </Col>
      </Row>

      {disk.volumes.map((vol) => (
        <VolumeTrendsChart key={vol.volume_id} volume_id={vol.volume_id} />
      ))}
    </Space>
  )
}

/** 来源小标签：smartctl（真盘）/ 系统探测（lsblk / CIM）/ USB 桥（真盘信息被隐藏）。 */
function SourceTag({ text }: { text: string }) {
  const color = text.includes('smartctl') ? 'geekblue' : text.includes('USB 桥') ? 'orange' : 'default'
  return (
    <Tooltip title={`信息来源：${text}`}>
      <Tag style={{ marginInlineStart: 8, fontSize: 11, lineHeight: '16px' }} color={color}>{text}</Tag>
    </Tooltip>
  )
}

function IdentityCard({ disk, liveParsed, liveLabel }: { disk: DiskDetail; liveParsed: ParsedSmart | null; liveLabel: string | null }) {
  // 真盘身份：优先现场读取，其次最近一次采集存的 smartctl 原始 JSON；都没有再回落系统探测 / USB 桥
  const smartIdentity = liveParsed
    ? { model: liveParsed.model, serial: liveParsed.serial, firmware: liveParsed.firmware, capacity_bytes: null as number | null }
    : parseSmartIdentity(disk.latest_smart?.raw_json)
  const smartSource = liveLabel ?? (smartIdentity ? 'smartctl' : null)

  const model = smartIdentity?.model || disk.physical_model || disk.bridge_model
  const modelSource = smartIdentity?.model
    ? smartSource
    : disk.physical_model ? '系统探测' : disk.bridge_model ? 'USB 桥' : null
  const serial = smartIdentity?.serial || disk.physical_serial
  const serialSource = smartIdentity?.serial ? smartSource : disk.physical_serial ? '系统探测' : null
  const identityHidden = !smartIdentity?.model && !smartIdentity?.serial && !disk.physical_model && !disk.physical_serial
  const capacity = smartIdentity?.capacity_bytes ?? disk.capacity_bytes
  const capacitySource = smartIdentity?.capacity_bytes != null ? smartSource : capacity != null ? '系统探测' : null

  return (
    <Card title="身份与容量" size="small">
      {identityHidden && (
        <Alert
          style={{ marginBottom: 12 }}
          type="warning"
          showIcon
          title="型号 / 序列号由 USB 桥隐藏，未读到真盘信息"
          description="USB 桥接盘常把 ATA IDENT 隔断。把盘接好（必要时换直连 SATA）后点右侧「现在读取 SMART」可尝试读取真盘身份。"
        />
      )}
      <Descriptions size="small" column={1} bordered>
        <Descriptions.Item label="磁盘 ID"><Text code>{disk.disk_id}</Text></Descriptions.Item>
        <Descriptions.Item label="真盘型号">
          <Space size={0} wrap>
            <Text>{model || '-'}</Text>
            {model && modelSource && <SourceTag text={modelSource} />}
          </Space>
        </Descriptions.Item>
        <Descriptions.Item label="真盘序列号">
          <Space size={0} wrap>
            {serial ? <Text code>{serial}</Text> : <Text>-</Text>}
            {serial && serialSource && <SourceTag text={serialSource} />}
          </Space>
        </Descriptions.Item>
        <Descriptions.Item label="固件版本">{smartIdentity?.firmware || '-'}</Descriptions.Item>
        <Descriptions.Item label="容量">
          <Space size={0} wrap>
            <Text>{capacity != null ? formatFileSize(capacity) : '-'}</Text>
            {capacity != null && capacitySource && <SourceTag text={capacitySource} />}
          </Space>
        </Descriptions.Item>
        <Descriptions.Item label="USB 桥 / 控制器">{disk.bridge_model || '-'}</Descriptions.Item>
        <Descriptions.Item label="接口类型">{disk.interface_type || '-'}</Descriptions.Item>
        <Descriptions.Item label="卷 / 快照数">{formatNumber(disk.volumes.length)} / {formatNumber(disk.snapshots.length)}</Descriptions.Item>
        <Descriptions.Item label="首次发现">{formatDateTime(disk.first_seen)}</Descriptions.Item>
        <Descriptions.Item label="最近发现">{formatDateTime(disk.last_seen)}</Descriptions.Item>
      </Descriptions>
    </Card>
  )
}

/** 可展开 + 可复制的原始输出块（SMART 失败原因 / smartctl 原始 JSON）。 */
function RawOutputBlock({ text, title }: { text: string; title: string }) {
  if (!text) return null
  return (
    <Collapse
      size="small"
      ghost
      items={[{
        key: 'raw',
        label: <Text type="secondary" style={{ fontSize: 12 }}>{title}</Text>,
        children: (
          <div>
            <div style={{ marginBottom: 4 }}>
              <Text copyable={{ text, tooltips: ['复制原始输出', '已复制'] }} style={{ fontSize: 12 }}>
                复制{title}
              </Text>
            </div>
            <pre style={{ maxHeight: 200, overflow: 'auto', margin: 0, padding: 8, fontSize: 12, background: '#fafafa', borderRadius: 4, whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
              {text}
            </pre>
          </div>
        ),
      }]}
    />
  )
}

function HealthCard({
  disk,
  readResult,
  readError,
  reading,
  onReadNow,
}: {
  disk: DiskDetail
  readResult: SmartReadResult | null
  readError: string | null
  reading: boolean
  onReadNow: () => void
}) {
  const smart = disk.latest_smart

  const healthTag = (health: string | null | undefined) => {
    if (health === 'passed') return <Tag color="success" icon={<CheckCircleOutlined />}>PASSED</Tag>
    if (health === 'failed') return <Tag color="error" icon={<ExclamationCircleOutlined />}>FAILED</Tag>
    return <Tag color="warning">未采集到</Tag>
  }

  const fieldsOf = (s: {
    health: string
    temperature_c: number | null
    power_on_hours: number | null
    reallocated_ct: number | null
    pending_ct: number | null
    start_stop_ct?: number | null
    spin_up_ms?: number | null
    device_type?: string | null
  }) => (
    <Descriptions size="small" column={2} bordered>
      <Descriptions.Item label="健康状态">{healthTag(s.health)}</Descriptions.Item>
      <Descriptions.Item label="温度">{s.temperature_c != null ? `${s.temperature_c} ℃` : '-'}</Descriptions.Item>
      <Descriptions.Item label="通电小时">{s.power_on_hours != null ? formatNumber(s.power_on_hours) : '-'}</Descriptions.Item>
      <Descriptions.Item label="启停次数">{s.start_stop_ct != null ? formatNumber(s.start_stop_ct) : '-'}</Descriptions.Item>
      <Descriptions.Item label="重分配扇区">{s.reallocated_ct != null ? formatNumber(s.reallocated_ct) : '-'}</Descriptions.Item>
      <Descriptions.Item label="待映射扇区">{s.pending_ct != null ? formatNumber(s.pending_ct) : '-'}</Descriptions.Item>
      {s.device_type != null && (
        <Descriptions.Item label="设备类型">{s.device_type || '-'}</Descriptions.Item>
      )}
    </Descriptions>
  )

  // 现场读取失败：原因 + 尝试记录，整段可复制
  const diagText = readResult && !readResult.ok
    ? [
        `原因：${readResult.message ?? '未知'}`,
        readResult.reason ? `类别：${readResult.reason}` : null,
        `设备：${readResult.device || '-'}`,
        ...readResult.attempts.map((a, i) =>
          `尝试 ${i + 1}（-d ${a.device_type}）：${a.error ? `错误 ${a.error}` : `退出码 ${a.rc ?? '-'}`}${a.stderr_excerpt ? `\n  ${a.stderr_excerpt}` : ''}`,
        ),
        readResult.raw_excerpt ? `输出片段：\n${readResult.raw_excerpt}` : null,
      ].filter(Boolean).join('\n')
    : null

  return (
    <Card
      title={
        <Space>
          <MedicineBoxOutlined />
          <span>健康状态</span>
        </Space>
      }
      size="small"
      extra={
        <Tooltip title="对当前插着的盘现场读一次 SMART（不写入快照 / 历史）">
          <Button size="small" icon={<ThunderboltOutlined />} loading={reading} onClick={onReadNow}>
            现在读取 SMART
          </Button>
        </Tooltip>
      }
    >
      <Space orientation="vertical" style={{ width: '100%' }} size="small">
        {readError && (
          <Alert
            type="warning"
            showIcon
            title="无法现场读取 SMART"
            description={
              <Space orientation="vertical" size={4} style={{ width: '100%' }}>
                <Text>{readError}</Text>
                <Text type="secondary">请确认这块盘当前已接到本机并可枚举（磁盘页 → 采集对话框里能看到），再重试。</Text>
              </Space>
            }
          />
        )}

        {readResult?.ok && readResult.parsed && (
          <>
            <Alert
              type="success"
              showIcon
              title="已读取（未写入快照 / 历史）"
              description={`设备 ${readResult.device}${readResult.device_type ? ` · 类型 ${readResult.device_type}` : ''}`}
            />
            {fieldsOf(readResult.parsed)}
            <RawOutputBlock text={readResult.raw_excerpt} title="原始输出" />
          </>
        )}

        {readResult && !readResult.ok && (
          <Alert
            type="error"
            showIcon
            title="现场读取失败"
            description={
              <Space orientation="vertical" size={6} style={{ width: '100%' }}>
                <Text strong>{readResult.message || '读取失败，原因未知'}</Text>
                {readResult.reason && <Text type="secondary">失败类别：{readResult.reason}</Text>}
                {readResult.attempts.length > 0 && (
                  <div>
                    <Text type="secondary">尝试记录（共 {readResult.attempts.length} 次）：</Text>
                    {readResult.attempts.map((a, i) => (
                      <div key={i} style={{ fontSize: 12, marginTop: 2 }}>
                        <Text code>-d {a.device_type}</Text>{' '}
                        {a.error ? <Text type="danger">错误：{a.error}</Text> : <Text type="secondary">退出码：{a.rc ?? '-'}</Text>}
                        {a.stderr_excerpt && <Text type="secondary"> · {a.stderr_excerpt}</Text>}
                      </div>
                    ))}
                  </div>
                )}
                <RawOutputBlock text={readResult.raw_excerpt} title="错误输出" />
                <Text copyable={{ text: diagText ?? '', tooltips: ['复制诊断信息', '已复制'] }} style={{ fontSize: 12 }}>
                  复制完整诊断信息
                </Text>
              </Space>
            }
          />
        )}

        {!readResult && !readError && smart && (
          <>
            <div>
              <Space size={0} wrap>
                {healthTag(smart.health)}
                <SourceTag text={smart.source === 'meta' ? '快照 meta 回填' : '采集历史（catalog）'} />
              </Space>
            </div>
            {fieldsOf(smart)}
            <Text type="secondary" style={{ fontSize: 12 }}>
              最近采集：{formatDateTime(smart.collected_at)}
              {smart.snapshot_id ? ` · ${smart.snapshot_id}` : ''}
            </Text>
            <RawOutputBlock text={smart.raw_json ?? ''} title="原始输出" />
          </>
        )}

        {!readResult && !readError && !smart && (
          <>
            {disk.smart_error ? (
              <Alert
                type="warning"
                showIcon
                title="上次采集未能读取 SMART"
                description={
                  <Space orientation="vertical" size={6} style={{ width: '100%' }}>
                    <Text>{disk.smart_error.smart_error}</Text>
                    <Text type="secondary" style={{ fontSize: 12 }}>来自快照 {disk.smart_error.snapshot_id}</Text>
                    <RawOutputBlock text={disk.smart_error.smart_error_raw ?? ''} title="错误详情" />
                  </Space>
                }
              />
            ) : (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={
                  <Space orientation="vertical" size="small" style={{ textAlign: 'left' }}>
                    <Text strong>未采集到 SMART</Text>
                    <Text>如需采集 SMART，请确保：</Text>
                    <ul style={{ margin: 0, paddingLeft: 16 }}>
                      <li>系统已安装 smartmontools</li>
                      <li>或在启动时设置环境变量 <Text code>CLDM_SMARTCTL</Text> 指向 smartctl 路径</li>
                      <li>USB 桥接盘通常需要加 <Text code>-d sat</Text> 参数</li>
                    </ul>
                  </Space>
                }
              />
            )}
            <Text type="secondary" style={{ fontSize: 12 }}>
              盘已接到本机时，点右上角「现在读取 SMART」可立即诊断并查看当前读数。
            </Text>
          </>
        )}
      </Space>
    </Card>
  )
}

function VolumesCard({ volumes }: { volumes: DiskVolume[] }) {
  const [selectedVolumeId, setSelectedVolumeId] = useState<string | null>(null)
  return (
    <Card
      title="卷列表"
      size="small"
      extra={volumes.length > 0 ? <Typography.Text type="secondary" style={{ fontSize: 12 }}>点击行查看卷详情</Typography.Text> : undefined}
    >
      {volumes.length === 0 ? (
        <Empty description="该盘没有注册卷" />
      ) : (
        <>
          <Table
            rowKey="volume_id"
            size="small"
            tableLayout="fixed"
            pagination={false}
            columns={[
              {
                title: '卷 ID',
                dataIndex: 'volume_id',
                ellipsis: true,
                render: (v: string) => (
                  <Tooltip title={v} placement="topLeft" mouseEnterDelay={0.3}>
                    <Button type="link" style={{ padding: 0, maxWidth: '100%' }} onClick={() => setSelectedVolumeId(v)}>
                      <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', display: 'block' }}>{v}</span>
                    </Button>
                  </Tooltip>
                ),
              },
              { title: '文件系统', dataIndex: 'filesystem', width: 90, ellipsis: true },
              { title: '标签', dataIndex: 'label', width: 130, ellipsis: true, render: (v: string | null) => v || '-' },
              { title: '容量', dataIndex: 'capacity_bytes', width: 110, align: 'right' as const, render: (v: number) => formatFileSize(v) },
            ]}
            dataSource={volumes}
            onRow={(record) => ({
              onClick: () => setSelectedVolumeId(record.volume_id),
              style: { cursor: 'pointer' },
            })}
          />
          <VolumeDetailDrawer volumeId={selectedVolumeId} onClose={() => setSelectedVolumeId(null)} />
        </>
      )}
    </Card>
  )
}

function SnapshotsCard({ snapshots, onSelect }: { snapshots: DiskSnapshot[]; onSelect: (id: string) => void }) {
  return (
    <Card title="快照时间线" size="small">
      {snapshots.length === 0 ? (
        <Empty description="该盘还没有快照" />
      ) : (
        <Table
          rowKey="snapshot_id"
          size="small"
          tableLayout="fixed"
          pagination={{ pageSize: 5, hideOnSinglePage: true }}
          scroll={{ x: 560 }}
          columns={[
            {
              title: '快照 ID',
              dataIndex: 'snapshot_id',
              ellipsis: true,
              width: 170,
              render: (v: string) => (
                <Tooltip title={v} placement="topLeft" mouseEnterDelay={0.3}>
                  <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', display: 'block' }}>{v}</span>
                </Tooltip>
              ),
            },
            { title: '采集时间', dataIndex: 'collected_at', render: (v: string) => formatDateTime(v), width: 150 },
            { title: '文件数', dataIndex: 'file_count', render: (v: number) => formatNumber(v), width: 90, align: 'right' },
            { title: '总大小', dataIndex: 'total_bytes', render: (v: number) => formatFileSize(v), width: 90, align: 'right' },
            {
              title: '操作',
              key: 'action',
              width: 60,
              render: (_: unknown, record: DiskSnapshot) => (
                <Button type="link" style={{ padding: 0 }} onClick={() => onSelect(record.snapshot_id)}>
                  查看
                </Button>
              ),
            },
          ]}
          dataSource={snapshots}
        />
      )}
    </Card>
  )
}
