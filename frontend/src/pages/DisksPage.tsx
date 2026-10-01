import { Card, Typography, Empty, Spin, Tag } from 'antd'
import { CheckCircleOutlined, ExclamationCircleOutlined } from '@ant-design/icons'
import { useDisks } from '../api/hooks'
import ErrorAlert from '../components/ErrorAlert'
import EllipsisText from '../components/EllipsisText'
import DiskDetailPanel from '../components/DiskDetailPanel'
import ResizableTable from '../components/ResizableTable'
import { formatFileSize, formatNumber, truncateMiddle } from '../utils/format'
import type { Disk } from '../api/client'

const { Title } = Typography

export default function DisksPage() {
  const { data, isLoading, error } = useDisks()

  const columns = [
    {
      title: '磁盘 ID',
      dataIndex: 'disk_id',
      key: 'disk_id',
      width: 170,
      ellipsis: true,
      render: (v: string) => (
        // 盘 ID 常由「型号 + 序号」拼接很长：中间截断保住头尾，悬浮看全值
        <EllipsisText value={truncateMiddle(v, 24)} code />
      ),
    },
    {
      title: '型号',
      dataIndex: 'physical_model',
      key: 'physical_model',
      ellipsis: true,
      render: (v: string | null) => <EllipsisText value={v} />,
    },
    {
      title: '序列号',
      dataIndex: 'physical_serial',
      key: 'physical_serial',
      width: 150,
      ellipsis: true,
      render: (v: string | null) => (
        <EllipsisText value={truncateMiddle(v ?? '-', 20)} code />
      ),
    },
    {
      title: 'USB 桥 / 控制器',
      dataIndex: 'bridge_model',
      key: 'bridge_model',
      width: 170,
      ellipsis: true,
      render: (v: string | null) => <EllipsisText value={v} />,
    },
    {
      title: '接口',
      dataIndex: 'interface_type',
      key: 'interface_type',
      width: 90,
      render: (v: string | null) => v || '-',
    },
    { title: '容量', dataIndex: 'capacity_bytes', key: 'capacity_bytes', width: 100, align: 'right' as const, render: (v: number) => formatFileSize(v) },
    {
      title: '健康',
      key: 'health',
      width: 90,
      render: (_: unknown, record: Disk) => {
        const health = record.latest_smart?.health
        if (health === 'passed') return <Tag color="success" icon={<CheckCircleOutlined />}>正常</Tag>
        if (health === 'failed') return <Tag color="error" icon={<ExclamationCircleOutlined />}>异常</Tag>
        return <Tag>未采集</Tag>
      },
    },
    { title: '卷数', dataIndex: 'volume_count', key: 'volume_count', width: 70, align: 'right' as const, render: (v: number) => formatNumber(v) },
    { title: '快照数', dataIndex: 'snapshot_count', key: 'snapshot_count', width: 80, align: 'right' as const, render: (v: number) => formatNumber(v) },
  ]

  return (
    <div>
      <Title level={4} style={{ marginTop: 0 }}>磁盘与卷</Title>
      {error && <ErrorAlert error={error} />}
      {isLoading && <Spin style={{ display: 'block', margin: '32px auto' }} />}
      <Card>
        {data?.items.length ? (
          <ResizableTable
            tableId="disks"
            rowKey="disk_id"
            size="small"
            tableLayout="fixed"
            columns={columns}
            dataSource={data.items}
            pagination={{ pageSize: 20 }}
            scroll={{ x: 'max-content' }}
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
