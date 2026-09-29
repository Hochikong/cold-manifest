import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Card, Empty, Segmented, Skeleton, Space, Typography } from 'antd'
import ReactECharts from 'echarts-for-react'
import type { EChartsOption } from 'echarts'
import { useVolumeTrends } from '../api/hooks'
import ErrorAlert from './ErrorAlert'
import { formatDateTime, formatFileSize, formatNumber } from '../utils/format'

const { Text } = Typography

const EMPTY_ARRAY: never[] = []

type MetricKey = 'file_count' | 'dir_count' | 'total_bytes'

interface MetricConfig {
  label: string
  unit: string
  format: (value: number) => string
  scale: (items: { file_count: number; dir_count: number; total_bytes: number }[]) => { values: number[]; unit: string }
}

const METRICS: Record<MetricKey, MetricConfig> = {
  file_count: {
    label: '文件数',
    unit: '个',
    format: formatNumber,
    scale: (items) => ({ values: items.map((i) => i.file_count), unit: '个' }),
  },
  dir_count: {
    label: '目录数',
    unit: '个',
    format: formatNumber,
    scale: (items) => ({ values: items.map((i) => i.dir_count), unit: '个' }),
  },
  total_bytes: {
    label: '总大小',
    unit: 'B',
    format: formatFileSize,
    scale: (items) => {
      const bytes = items.map((i) => i.total_bytes)
      const max = Math.max(...bytes, 0)
      const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
      let tier = 0
      let divisor = 1
      while (max / divisor >= 1024 && tier < units.length - 1) {
        divisor *= 1024
        tier++
      }
      return { values: bytes.map((b) => b / divisor), unit: units[tier] }
    },
  },
}

interface VolumeTrendsChartProps {
  volume_id: string
}

export default function VolumeTrendsChart({ volume_id }: VolumeTrendsChartProps) {
  const navigate = useNavigate()
  const [metric, setMetric] = useState<MetricKey>('file_count')
  const { data, isLoading, error } = useVolumeTrends(volume_id, 100)

  const items = data?.items ?? EMPTY_ARRAY

  const chartOption = useMemo<EChartsOption | null>(() => {
    if (items.length < 2) return null
    const scaled = METRICS[metric].scale(items)
    return {
      tooltip: {
        trigger: 'axis',
        formatter: (params: unknown) => {
          const p = (params as { axisValue: string; value: number }[])[0]
          const idx = items.findIndex((i) => i.collect_time === p.axisValue)
          const item = items[idx]
          if (!item) return ''
          const value =
            metric === 'file_count'
              ? item.file_count
              : metric === 'dir_count'
                ? item.dir_count
                : item.total_bytes
          return `${formatDateTime(item.collect_time)}<br/>${METRICS[metric].label}: ${METRICS[metric].format(value)}`
        },
      },
      grid: { left: 16, right: 24, top: 24, bottom: 24, containLabel: true },
      xAxis: {
        type: 'category',
        data: items.map((i) => i.collect_time),
        axisLabel: {
          formatter: (value: string) => formatDateTime(value),
          rotate: 30,
        },
      },
      yAxis: {
        type: 'value',
        name: scaled.unit,
        axisLabel: {
          formatter: (value: number) => formatNumber(value),
        },
      },
      series: [
        {
          type: 'line',
          data: scaled.values,
          smooth: true,
          symbol: 'circle',
          symbolSize: 8,
          itemStyle: { color: '#1677ff' },
          lineStyle: { width: 3 },
          areaStyle: {
            color: {
              type: 'linear',
              x: 0,
              y: 0,
              x2: 0,
              y2: 1,
              colorStops: [
                { offset: 0, color: 'rgba(22, 119, 255, 0.25)' },
                { offset: 1, color: 'rgba(22, 119, 255, 0.02)' },
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
      <Card title="历次快照趋势" size="small">
        <Skeleton active paragraph={{ rows: 6 }} />
      </Card>
    )
  }

  if (items.length === 0) {
    return (
      <Card title="历次快照趋势" size="small">
        <Empty description="该卷还没有快照" />
      </Card>
    )
  }

  return (
    <Card
      title="历次快照趋势"
      size="small"
      extra={
        <Segmented<MetricKey>
          value={metric}
          onChange={setMetric}
          options={[
            { label: '文件数', value: 'file_count' },
            { label: '目录数', value: 'dir_count' },
            { label: '总大小', value: 'total_bytes' },
          ]}
        />
      }
    >
      {items.length === 1 ? (
        <Space orientation="vertical" style={{ width: '100%' }}>
          <Text type="secondary">该卷目前只有 1 次快照。</Text>
          <Text>需 ≥2 次快照才能显示趋势；后续采集新快照后会自动出现折线图。</Text>
        </Space>
      ) : (
        <ReactECharts
          option={chartOption}
          style={{ height: 320 }}
          onEvents={{
            click: (params: unknown) => {
              const p = params as { componentType: string; name: string }
              if (p.componentType !== 'series') return
              const item = items.find((i) => i.collect_time === p.name)
              if (!item) return
              navigate(`/snapshots?snapshot=${encodeURIComponent(item.snapshot_id)}&tab=overview`)
            },
          }}
        />
      )}
    </Card>
  )
}
