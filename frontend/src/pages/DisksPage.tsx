import { Card, Table, Typography, Empty, Spin } from 'antd'
import { useDisks } from '../api/hooks'
import ErrorAlert from '../components/ErrorAlert'
import DiskDetailPanel from '../components/DiskDetailPanel'
import { formatFileSize, formatNumber } from '../utils/format'
import type { Disk } from '../api/client'

const { Title } = Typography

export default function DisksPage() {
  const { data, isLoading, error } = useDisks()

  const columns = [
    { title: '磁盘 ID', dataIndex: 'disk_id', key: 'disk_id', ellipsis: true },
    { title: '型号', dataIndex: 'physical_model', key: 'physical_model', render: (v: string | null) => v || '-' },
    { title: '序列号', dataIndex: 'physical_serial', key: 'physical_serial', render: (v: string | null) => v || '-' },
    { title: 'USB 桥 / 控制器', dataIndex: 'bridge_model', key: 'bridge_model', render: (v: string | null) => v || '-' },
    { title: '接口', dataIndex: 'interface_type', key: 'interface_type', render: (v: string | null) => v || '-' },
    { title: '容量', dataIndex: 'capacity_bytes', key: 'capacity_bytes', render: (v: number) => formatFileSize(v) },
    { title: '卷数', dataIndex: 'volume_count', key: 'volume_count', render: (v: number) => formatNumber(v) },
    { title: '快照数', dataIndex: 'snapshot_count', key: 'snapshot_count', render: (v: number) => formatNumber(v) },
  ]

  return (
    <div>
      <Title level={4} style={{ marginTop: 0 }}>磁盘与卷</Title>
      {error && <ErrorAlert error={error} />}
      {isLoading && <Spin style={{ display: 'block', margin: '32px auto' }} />}
      <Card>
        {data?.items.length ? (
          <Table
            rowKey="disk_id"
            size="small"
            columns={columns}
            dataSource={data.items}
            pagination={{ pageSize: 20 }}
            expandable={{
              expandedRowRender: (record: Disk) => <DiskDetailPanel disk_id={record.disk_id} />,
              rowExpandable: () => true,
            }}
          />
        ) : !isLoading ? (
          <Empty description="尚未发现磁盘" />
        ) : null}
      </Card>
    </div>
  )
}
