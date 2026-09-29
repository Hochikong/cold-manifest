import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Card,
  Row,
  Col,
  Statistic,
  Button,
  Table,
  Timeline,
  Typography,
  Space,
  Empty,
  Spin,
  Badge,
  Tag,
} from 'antd'
import {
  HddOutlined,
  DatabaseOutlined,
  ClockCircleOutlined,
  SwapOutlined,
  PlusOutlined,
  ImportOutlined,
} from '@ant-design/icons'
import { useVolumes, useSnapshots, useDiffs } from '../api/hooks'
import ErrorAlert from '../components/ErrorAlert'
import ImportDialog from '../components/ImportDialog'
import CollectDialog from '../components/CollectDialog'
import { formatFileSize, formatDateTime } from '../utils/format'

const { Title } = Typography

export default function OverviewPage() {
  const navigate = useNavigate()
  const [importOpen, setImportOpen] = useState(false)
  const [collectOpen, setCollectOpen] = useState(false)
  const { data: volumes, isLoading: volLoading, error: volError } = useVolumes()
  const { data: snapshots, isLoading: snapLoading, error: snapError } = useSnapshots()
  const { data: diffs, isLoading: diffLoading, error: diffError } = useDiffs()

  const latestSnapshot = useMemo(() => {
    if (!snapshots?.items.length) return null
    return [...snapshots.items].sort((a, b) =>
      new Date(b.collected_at).getTime() - new Date(a.collected_at).getTime()
    )[0]
  }, [snapshots])

  const loading = volLoading || snapLoading || diffLoading
  const error = volError || snapError || diffError

  const volumeColumns = [
    {
      title: '卷 ID',
      dataIndex: 'volume_id',
      key: 'volume_id',
      ellipsis: true,
    },
    {
      title: '型号',
      dataIndex: 'physical_model',
      key: 'physical_model',
      ellipsis: true,
    },
    {
      title: '容量',
      dataIndex: 'capacity_bytes',
      key: 'capacity_bytes',
      render: (v: number) => formatFileSize(v),
    },
    {
      title: '文件系统',
      dataIndex: 'filesystem',
      key: 'filesystem',
    },
    {
      title: '快照数',
      dataIndex: 'snapshot_count',
      key: 'snapshot_count',
    },
    {
      title: '操作',
      key: 'action',
      render: (_: unknown, record: { volume_id: string }) => (
        <Button type="link" onClick={() => navigate(`/disks?volume=${encodeURIComponent(record.volume_id)}`)}>
          详情
        </Button>
      ),
    },
  ]

  const timelineGroups = useMemo(() => {
    if (!snapshots?.items.length) return []
    const byVol: Record<string, typeof snapshots.items> = {}
    snapshots.items.forEach((s) => {
      if (!byVol[s.volume_id]) byVol[s.volume_id] = []
      byVol[s.volume_id].push(s)
    })
    return Object.entries(byVol).map(([volume_id, items]) => ({
      volume_id,
      items: [...items].sort((a, b) => new Date(a.collected_at).getTime() - new Date(b.collected_at).getTime()),
    }))
  }, [snapshots])

  return (
    <div>
      <Title level={4} style={{ marginTop: 0 }}>总览</Title>
      {error && <ErrorAlert error={error} />}
      {loading && <Spin style={{ display: 'block', margin: '32px auto' }} />}

      <Row gutter={[16, 16]}>
        <Col xs={24} sm={12} md={6}>
          <Card>
            <Statistic
              title="磁盘 / 卷"
              value={volumes?.count ?? 0}
              prefix={<HddOutlined />}
            />
          </Card>
        </Col>
        <Col xs={24} sm={12} md={6}>
          <Card>
            <Statistic
              title="快照总数"
              value={snapshots?.count ?? 0}
              prefix={<DatabaseOutlined />}
            />
          </Card>
        </Col>
        <Col xs={24} sm={12} md={6}>
          <Card>
            <Statistic
              title="最近一次采集"
              value={latestSnapshot ? formatDateTime(latestSnapshot.collected_at) : '无'}
              prefix={<ClockCircleOutlined />}
            />
          </Card>
        </Col>
        <Col xs={24} sm={12} md={6}>
          <Card>
            <Statistic
              title="历史对比任务"
              value={diffs?.count ?? 0}
              prefix={<SwapOutlined />}
            />
          </Card>
        </Col>
      </Row>

      <Card style={{ marginTop: 16 }}>
        <Space>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setCollectOpen(true)}>
            开始采集
          </Button>
          <Button icon={<ImportOutlined />} onClick={() => setImportOpen(true)}>
            导入旧快照
          </Button>
        </Space>
      </Card>

      <ImportDialog open={importOpen} onClose={() => setImportOpen(false)} />
      <CollectDialog open={collectOpen} onClose={() => setCollectOpen(false)} />

      <Card title="磁盘与卷" style={{ marginTop: 16 }}>
        {volumes?.count ? (
          <Table
            rowKey="volume_id"
            size="small"
            columns={volumeColumns}
            dataSource={volumes.items}
            pagination={false}
          />
        ) : !volLoading ? (
          <Empty description="尚未发现磁盘" />
        ) : null}
      </Card>

      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col xs={24} lg={12}>
          <Card title="快照时间线">
            {snapLoading ? (
              <Spin style={{ display: 'block', margin: '32px auto' }} />
            ) : timelineGroups.length ? (
              timelineGroups.map((group) => (
                <div key={group.volume_id} style={{ marginBottom: 16 }}>
                  <Typography.Text strong>{group.volume_id}</Typography.Text>
                  <Timeline
                    mode="start"
                    items={group.items.map((s) => ({
                      title: formatDateTime(s.collected_at),
                      content: (
                        <Button
                          type="link"
                          style={{ padding: 0 }}
                          onClick={() =>
                            navigate(`/snapshots?snapshot=${encodeURIComponent(s.snapshot_id)}`)
                          }
                        >
                          {s.snapshot_id.split('/')[1]}
                        </Button>
                      ),
                      icon: <Badge color="#aa3bff" />,
                    }))}
                  />
                </div>
              ))
            ) : (
              <Empty description="暂无快照" />
            )}
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card title="最近对比">
            {diffLoading ? (
              <Spin style={{ display: 'block', margin: '32px auto' }} />
            ) : diffs?.items.length ? (
              <Timeline
                items={diffs.items.slice(0, 10).map((d) => ({
                  title: formatDateTime(d.created_at),
                  content: (
                    <Space orientation="vertical" size={0}>
                      <Button type="link" style={{ padding: 0 }} onClick={() => navigate(`/diff?id=${d.diff_id}`)}>
                        {d.diff_id}
                      </Button>
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        {d.a.split('/')[1]} → {d.b.split('/')[1]}
                      </Typography.Text>
                      <Space size={4}>
                        <Tag color="green">+{d.summary?.added ?? 0}</Tag>
                        <Tag color="red">-{d.summary?.removed ?? 0}</Tag>
                        <Tag color="orange">~{d.summary?.size_changed ?? 0}</Tag>
                      </Space>
                    </Space>
                  ),
                }))}
              />
            ) : (
              <Empty description="暂无对比记录" />
            )}
          </Card>
        </Col>
      </Row>
    </div>
  )
}
