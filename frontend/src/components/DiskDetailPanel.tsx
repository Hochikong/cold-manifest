import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Card, Descriptions, Empty, Row, Col, Skeleton, Space, Table, Tag, Typography, Button } from 'antd'
import { CheckCircleOutlined, ExclamationCircleOutlined, MedicineBoxOutlined } from '@ant-design/icons'
import { useDisk } from '../api/hooks'
import ErrorAlert from './ErrorAlert'
import SmartTrendsChart from './SmartTrendsChart'
import VolumeTrendsChart from './VolumeTrendsChart'
import VolumeDetailDrawer from './VolumeDetailDrawer'
import { formatDateTime, formatFileSize, formatNumber } from '../utils/format'
import type { DiskVolume, DiskSnapshot, SmartItem } from '../api/client'

const { Text } = Typography

interface DiskDetailPanelProps {
  disk_id: string
}

export default function DiskDetailPanel({ disk_id }: DiskDetailPanelProps) {
  const navigate = useNavigate()
  const { data: disk, isLoading, error } = useDisk(disk_id)

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

  const smart = disk.latest_smart

  return (
    <Space orientation="vertical" style={{ width: '100%' }} size="middle">
      <Row gutter={[16, 16]}>
        <Col xs={24} lg={12}>
          <IdentityCard disk={disk} volumeCount={disk.volumes.length} snapshotCount={disk.snapshots.length} />
        </Col>
        <Col xs={24} lg={12}>
          <HealthCard smart={smart} />
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

function IdentityCard({ disk, volumeCount, snapshotCount }: { disk: { disk_id: string; physical_model: string | null; physical_serial: string | null; bridge_model: string | null; interface_type: string | null; capacity_bytes: number; first_seen: string; last_seen: string }; volumeCount: number; snapshotCount: number }) {
  return (
    <Card title="身份与容量" size="small">
      <Descriptions size="small" column={1} bordered>
        <Descriptions.Item label="磁盘 ID">{disk.disk_id}</Descriptions.Item>
        <Descriptions.Item label="型号">{disk.physical_model || '-'}</Descriptions.Item>
        <Descriptions.Item label="序列号">{disk.physical_serial || '-'}</Descriptions.Item>
        <Descriptions.Item label="USB 桥 / 控制器">{disk.bridge_model || '-'}</Descriptions.Item>
        <Descriptions.Item label="接口类型">{disk.interface_type || '-'}</Descriptions.Item>
        <Descriptions.Item label="容量">{formatFileSize(disk.capacity_bytes)}</Descriptions.Item>
        <Descriptions.Item label="卷数">{formatNumber(volumeCount)}</Descriptions.Item>
        <Descriptions.Item label="快照数">{formatNumber(snapshotCount)}</Descriptions.Item>
        <Descriptions.Item label="首次发现">{formatDateTime(disk.first_seen)}</Descriptions.Item>
        <Descriptions.Item label="最近发现">{formatDateTime(disk.last_seen)}</Descriptions.Item>
      </Descriptions>
    </Card>
  )
}

function HealthCard({ smart }: { smart: SmartItem | { health: string; temperature_c: number | null; power_on_hours: number | null; reallocated_ct: number | null; pending_ct: number | null; collected_at: string } | null }) {
  if (!smart) {
    return (
      <Card title="健康状态" size="small">
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={
            <Space orientation="vertical" size="small" style={{ textAlign: 'left' }}>
              <Text strong>未采集到 SMART</Text>
              <Text type="secondary">当前没有该盘的健康数据。</Text>
              <Text>如需采集 SMART，请确保：</Text>
              <ul style={{ margin: 0, paddingLeft: 16 }}>
                <li>系统已安装 smartmontools</li>
                <li>或在启动时设置环境变量 <Text code>CLDM_SMARTCTL</Text> 指向 smartctl 路径</li>
                <li>USB 桥接盘通常需要加 <Text code>-d sat</Text> 参数</li>
              </ul>
            </Space>
          }
        />
      </Card>
    )
  }

  const isHealthy = smart.health === 'passed'

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
        <Tag color={isHealthy ? 'success' : 'error'} icon={isHealthy ? <CheckCircleOutlined /> : <ExclamationCircleOutlined />}>
          {smart.health === 'passed' ? '正常' : smart.health}
        </Tag>
      }
    >
      <Descriptions size="small" column={2} bordered>
        <Descriptions.Item label="温度">{smart.temperature_c != null ? `${smart.temperature_c} ℃` : '-'}</Descriptions.Item>
        <Descriptions.Item label="通电小时">{smart.power_on_hours != null ? formatNumber(smart.power_on_hours) : '-'}</Descriptions.Item>
        <Descriptions.Item label="重映射扇区">{smart.reallocated_ct != null ? formatNumber(smart.reallocated_ct) : '-'}</Descriptions.Item>
        <Descriptions.Item label="待映射扇区">{smart.pending_ct != null ? formatNumber(smart.pending_ct) : '-'}</Descriptions.Item>
        {'device_type' in smart && smart.device_type != null && (
          <Descriptions.Item label="设备类型">{smart.device_type}</Descriptions.Item>
        )}
        <Descriptions.Item label="最近采集">{formatDateTime(smart.collected_at)}</Descriptions.Item>
      </Descriptions>
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
            pagination={false}
            columns={[
              { title: '卷 ID', dataIndex: 'volume_id', ellipsis: true, render: (v: string) => <Button type="link" style={{ padding: 0 }} onClick={() => setSelectedVolumeId(v)}>{v}</Button> },
              { title: '文件系统', dataIndex: 'filesystem' },
              { title: '标签', dataIndex: 'label', render: (v: string | null) => v || '-' },
              { title: '容量', dataIndex: 'capacity_bytes', render: (v: number) => formatFileSize(v) },
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
          pagination={{ pageSize: 5, hideOnSinglePage: true }}
          scroll={{ x: 520 }}
          columns={[
            { title: '快照 ID', dataIndex: 'snapshot_id', ellipsis: true, width: 170 },
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
