import { Card, Table, Typography, Empty, Spin } from 'antd'
import { useVolumes } from '../api/hooks'
import ErrorAlert from '../components/ErrorAlert'
import VolumeTrendsChart from '../components/VolumeTrendsChart'
import { formatFileSize, formatNumber } from '../utils/format'

const { Title } = Typography

export default function DisksPage() {
  const { data, isLoading, error } = useVolumes()

  const columns = [
    { title: '卷 ID', dataIndex: 'volume_id', key: 'volume_id', ellipsis: true },
    { title: '磁盘 ID', dataIndex: 'disk_id', key: 'disk_id', ellipsis: true },
    { title: '型号', dataIndex: 'physical_model', key: 'physical_model', ellipsis: true },
    { title: '序列号', dataIndex: 'physical_serial', key: 'physical_serial', render: (v: string | null) => v || '-' },
    { title: '文件系统', dataIndex: 'filesystem', key: 'filesystem' },
    { title: '容量', dataIndex: 'capacity_bytes', key: 'capacity_bytes', render: (v: number) => formatFileSize(v) },
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
            rowKey="volume_id"
            size="small"
            columns={columns}
            dataSource={data.items}
            pagination={{ pageSize: 20 }}
            scroll={{ x: 'max-content' }}
            expandable={{
              expandedRowRender: (record) => <VolumeTrendsChart volume_id={record.volume_id} />,
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
