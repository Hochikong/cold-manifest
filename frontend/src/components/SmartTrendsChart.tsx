import { useMemo, useState } from 'react'
import { Card, Empty, Segmented, Skeleton, Space, Typography } from 'antd'
import ReactECharts from 'echarts-for-react'
import type { EChartsOption } from 'echarts'
import ErrorAlert from './ErrorAlert'
import { useDiskSmartHistory } from '../api/hooks'
import { formatDateTime, formatNumber } from '../utils/format'

const { Text } = Typography

const EMPTY_ARRAY: never[] = []

type MetricKey = 'temperature_c' | 'power_on_hours'

interface MetricConfig {
  label: string
  unit: string
  format: (value: number) => string
}

const METRICS: Record<MetricKey, MetricConfig> = {
  temperature_c: {
    label: '温度',
    unit: '℃',
    format: (value) => `${value} ℃`,
  },
  power_on_hours: {
    label: '通电小时',
    unit: 'h',
    format: formatNumber,
  },
}

interface SmartTrendsChartProps {
  disk_id: string
}

export default function SmartTrendsChart({ disk_id }: SmartTrendsChartProps) {
  const [metric, setMetric] = useState<MetricKey>('temperature_c')
  const { data, isLoading, error } = useDiskSmartHistory(disk_id)

  const items = data?.items ?? EMPTY_ARRAY

  const chartOption = useMemo<EChartsOption | null>(() => {
    const validItems = items.filter((i) => i[metric] != null)
    if (validItems.length < 2) return null
    return {
      tooltip: {
        trigger: 'axis',
        formatter: (params: unknown) => {
          const p = (params as { axisValue: string; value: number }[])[0]
          const item = validItems.find((i) => i.collected_at === p.axisValue)
          if (!item) return ''
          const value = item[metric] as number
          return `${formatDateTime(item.collected_at)}<br/>${METRICS[metric].label}: ${METRICS[metric].format(value)}`
        },
      },
      grid: { left: 16, right: 24, top: 24, bottom: 24, containLabel: true },
      xAxis: {
        type: 'category',
        data: validItems.map((i) => i.collected_at),
        axisLabel: {
          formatter: (value: string) => formatDateTime(value),
          rotate: 30,
        },
      },
      yAxis: {
        type: 'value',
        name: METRICS[metric].unit,
        axisLabel: {
          formatter: (value: number) => formatNumber(value),
        },
      },
      series: [
        {
          type: 'line',
          data: validItems.map((i) => i[metric] as number),
          smooth: true,
          symbol: 'circle',
          symbolSize: 8,
          itemStyle: { color: metric === 'temperature_c' ? '#fa8c16' : '#1677ff' },
          lineStyle: { width: 3 },
          areaStyle: {
            color: {
              type: 'linear',
              x: 0,
              y: 0,
              x2: 0,
              y2: 1,
              colorStops: [
                { offset: 0, color: metric === 'temperature_c' ? 'rgba(250, 140, 22, 0.25)' : 'rgba(22, 119, 255, 0.25)' },
                { offset: 1, color: metric === 'temperature_c' ? 'rgba(250, 140, 22, 0.02)' : 'rgba(22, 119, 255, 0.02)' },
              ],
            },
          },
        },
      ],
    }
  }, [items, metric])

  if (error) {
    return <ErrorAlert error={error} />
  }

  if (isLoading) {
    return (
      <Card title="SMART 趋势" size="small">
        <Skeleton active paragraph={{ rows: 6 }} />
      </Card>
    )
  }

  if (items.length === 0) {
    return (
      <Card title="SMART 趋势" size="small">
        <Empty description="暂无 SMART 历史数据" />
      </Card>
    )
  }

  const validCount = items.filter((i) => i[metric] != null).length

  return (
    <Card
      title="SMART 趋势"
      size="small"
      extra={
        <Segmented<MetricKey>
          value={metric}
          onChange={setMetric}
          options={[
            { label: '温度', value: 'temperature_c' },
            { label: '通电小时', value: 'power_on_hours' },
          ]}
        />
      }
    >
      {validCount < 2 ? (
        <Space orientation="vertical" style={{ width: '100%' }}>
          <Text type="secondary">该指标目前只有 {validCount} 个有效点。</Text>
          <Text>需 ≥2 个有效数据点才能显示趋势。</Text>
        </Space>
      ) : (
        <ReactECharts option={chartOption} style={{ height: 320 }} />
      )}
    </Card>
  )
}
