import { Card, Result, Typography } from 'antd'
import { ClockCircleOutlined } from '@ant-design/icons'

const { Paragraph } = Typography

export default function TasksPage() {
  return (
    <Card>
      <Result
        icon={<ClockCircleOutlined />}
        title="任务"
        subTitle="P1 提供"
      >
        <Paragraph type="secondary">
          采集、导入、导出等任务的配置与进度管理将在 P1 阶段实现。
        </Paragraph>
      </Result>
    </Card>
  )
}
