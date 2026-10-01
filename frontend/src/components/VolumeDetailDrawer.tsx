import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Button, Descriptions, Drawer, Empty, Space, Spin, Tabs, Tag, Typography } from 'antd'
import type { TableProps } from 'antd'
import { useVolumeDetail } from '../api/hooks'
import type { VolumeDetailSnapshot } from '../api/client'
import ErrorAlert from './ErrorAlert'
import VolumeTrendsChart from './VolumeTrendsChart'
import ResizableTable from './ResizableTable'
import { formatDateTime, formatFileSize, formatNumber } from '../utils/format'

const { Text } = Typography

interface VolumeDetailDrawerProps {
  volumeId: string | null
  onClose: () => void
}

const STATUS_COLORS: Record<string, string> = {
  sealed: 'success',
  incomplete: 'warning',
  importing: 'processing',
}

export default function VolumeDetailDrawer({ volumeId, onClose }: VolumeDetailDrawerProps) {
  const navigate = useNavigate()
  const [tab, setTab] = useState('identity')
  const { data: vol, isLoading, error } = useVolumeDetail(volumeId ?? undefined)

  const close = () => {
    setTab('identity')
    onClose()
  }

  const snapshotColumns: TableProps<VolumeDetailSnapshot>['columns'] = [
    {
      title: '快照 ID',
      dataIndex: 'snapshot_id',
      key: 'snapshot_id',
      ellipsis: true,
      render: (v: string, record: VolumeDetailSnapshot) => {
        const ts = v.split('/')[1]
        return (
          <Space size={6}>
            <Text code>{ts ?? v}</Text>
            {record.status !== 'sealed' && <Tag color={STATUS_COLORS[record.status] ?? 'default'}>{record.status}</Tag>}
          </Space>
        )
      },
    },
    { title: '采集时间', dataIndex: 'collected_at', key: 'collected_at', width: 150, render: (v: string | null) => formatDateTime(v) },
    { title: '文件数', dataIndex: 'file_count', key: 'file_count', width: 90, align: 'right', render: (v: number | null) => formatNumber(v ?? 0) },
    { title: '总大小', dataIndex: 'total_bytes', key: 'total_bytes', width: 100, align: 'right', render: (v: number | null) => formatFileSize(v ?? 0) },
    {
      title: '操作',
      key: 'action',
      width: 60,
      render: (_: unknown, record: VolumeDetailSnapshot) => (
        <Button
          type="link"
          style={{ padding: 0 }}
          onClick={() => navigate(`/snapshots?snapshot=${encodeURIComponent(record.snapshot_id)}&tab=overview`)}
        >
          查看
        </Button>
      ),
    },
  ]

  return (
    <Drawer title={volumeId ? `卷详情：${volumeId}` : '卷详情'} size={680} open={!!volumeId} onClose={close}>
      {error && <ErrorAlert error={error} />}
      {isLoading && <Spin style={{ display: 'block', margin: '32px auto' }} />}
      {vol && (
        <Tabs
          activeKey={tab}
          onChange={setTab}
          items={[
            {
              key: 'identity',
              label: '身份',
              children: (
                <Descriptions size="small" column={1} bordered>
                  <Descriptions.Item label="卷 ID">{vol.volume_id}</Descriptions.Item>
                  <Descriptions.Item label="磁盘 ID">{vol.disk_id}</Descriptions.Item>
                  <Descriptions.Item label="磁盘型号">{vol.physical_model || '-'}</Descriptions.Item>
                  <Descriptions.Item label="磁盘序列号">{vol.physical_serial || '-'}</Descriptions.Item>
                  <Descriptions.Item label="分区序号">{vol.partition_index ?? '-'}</Descriptions.Item>
                  <Descriptions.Item label="分区 UUID">{vol.partition_uuid || '-'}</Descriptions.Item>
                  <Descriptions.Item label="卷序列号">{vol.volume_serial_hex || '-'}</Descriptions.Item>
                  <Descriptions.Item label="文件系统">{vol.filesystem}</Descriptions.Item>
                  <Descriptions.Item label="卷标">{vol.label || '-'}</Descriptions.Item>
                  <Descriptions.Item label="卷容量">{formatFileSize(vol.capacity_bytes)}</Descriptions.Item>
                  <Descriptions.Item label="磁盘容量">{formatFileSize(vol.disk_capacity_bytes)}</Descriptions.Item>
                  <Descriptions.Item label="备注">{vol.notes || '-'}</Descriptions.Item>
                </Descriptions>
              ),
            },
            {
              key: 'snapshots',
              label: `快照（${vol.snapshots.length}）`,
              children: vol.snapshots.length ? (
                <ResizableTable<VolumeDetailSnapshot>
                  tableId="volume-snapshots"
                  rowKey="snapshot_id"
                  size="small"
                  tableLayout="fixed"
                  columns={snapshotColumns}
                  dataSource={vol.snapshots}
                  pagination={{ pageSize: 8, hideOnSinglePage: true }}
                  scroll={{ x: 'max-content' }}
                  onRow={(record) => ({
                    onClick: () => navigate(`/snapshots?snapshot=${encodeURIComponent(record.snapshot_id)}&tab=overview`),
                    style: { cursor: 'pointer' },
                  })}
                />
              ) : (
                <Empty description="该卷还没有快照" />
              ),
            },
            {
              key: 'trends',
              label: '趋势图',
              children: <VolumeTrendsChart volume_id={vol.volume_id} />,
            },
          ]}
        />
      )}
    </Drawer>
  )
}
