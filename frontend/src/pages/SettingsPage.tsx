import { Card, Descriptions, Spin, Typography } from 'antd'
import { useSettings } from '../api/hooks'
import ErrorAlert from '../components/ErrorAlert'

const { Title } = Typography

export default function SettingsPage() {
  const { data, isLoading, error } = useSettings()

  return (
    <div>
      <Title level={4} style={{ marginTop: 0 }}>设置</Title>
      {error && <ErrorAlert error={error} />}
      {isLoading && <Spin style={{ display: 'block', margin: '32px auto' }} />}
      <Card>
        <Descriptions bordered column={1}>
          <Descriptions.Item label="版本">{data?.version || '-'}</Descriptions.Item>
          <Descriptions.Item label="数据根目录">{data?.data_root || '-'}</Descriptions.Item>
        </Descriptions>
      </Card>
    </div>
  )
}
