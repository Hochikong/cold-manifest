import { useMemo, useState } from 'react'
import { Card, Empty, Segmented, Skeleton, Space, Typography } from 'antd'
import ReactECharts from 'echarts-for-react'
import type { EChartsOption } from 'echarts'
import ErrorAlert from './ErrorAlert'
import { useDiskSmartHistory } from '../api/hooks'
import { formatDateTime, formatNumber } from '../utils/format'
import type { SmartItem, SsdMetrics } from '../api/client'

const { Text } = Typography

const EMPTY_ARRAY: never[] = []

type MetricKey = 'temperature_c' | 'power_on_hours' | 'life_left_pct' | 'written_tb' | 'reallocated_ct' | 'udma_crc_errors'

/** 单个历史点的指标取值：字段缺失 → null（该点跳过）。 */
type ValueFn = (item: SmartItem) => number | null

interface MetricConfig {
  label: string
  unit: string
  format: (value: number) => string
  value: ValueFn
  color: string
  areaColor: string
}

/** 字节 → TB（历史点只有 written_bytes 没有换算好的 written_tb 时用）。 */
const bytesToTb = (b: number | null | undefined): number | null =>
  b == null || Number.isNaN(b) ? null : b / 1024 ** 4

/** 剩余寿命 %：life_left_pct 优先，缺则 100 - percentage_used。 */
const lifeLeftOf = (ssd: SsdMetrics | null | undefined): number | null => {
  if (!ssd) return null
  if (ssd.life_left_pct != null) return ssd.life_left_pct
  if (ssd.percentage_used != null) return 100 - ssd.percentage_used
  return null
}

/** 温度：temperature_c 优先（全盘通用），缺则用 SSD 第一探头。 */
const tempOf = (item: SmartItem): number | null => {
  if (item.temperature_c != null) return item.temperature_c
  return item.ssd?.temp_sensors?.[0] ?? null
}

/** 写入量轴标签：TB 为基准，<1 TB 自动切 GB。 */
const formatTbAxis = (v: number): string =>
  Math.abs(v) >= 1 ? `${Math.round(v * 10) / 10} TB` : `${Math.round(v * 1024)} GB`

const METRICS: Record<MetricKey, MetricConfig> = {
  temperature_c: {
    label: '温度',
    unit: '℃',
    format: (value) => `${value} ℃`,
    value: tempOf,
    color: '#fa8c16',
    areaColor: 'rgba(250, 140, 22, 0.25)',
  },
  power_on_hours: {
    label: '通电小时',
    unit: 'h',
    format: formatNumber,
    value: (i) => i.power_on_hours,
    color: '#1677ff',
    areaColor: 'rgba(22, 119, 255, 0.25)',
  },
  life_left_pct: {
    label: '寿命剩余 %',
    unit: '%',
    format: (value) => `${Math.round(value * 10) / 10} %`,
    value: (i) => lifeLeftOf(i.ssd),
    color: '#52c41a',
    areaColor: 'rgba(82, 196, 26, 0.25)',
  },
  written_tb: {
    label: '累计写入量',
    unit: 'TB',
    format: (value) => `${Math.round(value * 100) / 100} TB`,
    value: (i) => i.ssd?.written_tb ?? bytesToTb(i.ssd?.written_bytes),
    color: '#722ed1',
    areaColor: 'rgba(114, 46, 209, 0.25)',
  },
  reallocated_ct: {
    label: '重分配扇区',
    unit: '',
    format: formatNumber,
    value: (i) => i.reallocated_ct,
    color: '#f5222d',
    areaColor: 'rgba(245, 34, 45, 0.25)',
  },
  udma_crc_errors: {
    label: 'UDMA CRC',
    unit: '',
    format: formatNumber,
    value: (i) => i.udma_crc_errors ?? null,
    color: '#faad14',
    areaColor: 'rgba(250, 173, 20, 0.25)',
  },
}

interface SmartTrendsChartProps {
  disk_id: string
}

export default function SmartTrendsChart({ disk_id }: SmartTrendsChartProps) {
  const [metric, setMetric] = useState<MetricKey>('temperature_c')
  const { data, isLoading, error } = useDiskSmartHistory(disk_id)

  const items = data?.items ?? EMPTY_ARRAY

  // ATA 机械盘专属指标：历史里出现至少一个有效值才给选项（NVMe 盘两项恒空 → 不显示）
  const hasReallocHistory = items.some((i) => i.reallocated_ct != null)
  const hasCrcHistory = items.some((i) => i.udma_crc_errors != null)

  const chartOption = useMemo<EChartsOption | null>(() => {
    const cfg = METRICS[metric]
    const validItems = items.filter((i) => cfg.value(i) != null)
    if (validItems.length < 2) return null
    return {
      tooltip: {
        trigger: 'axis',
        formatter: (params: unknown) => {
          const p = (params as { axisValue: string; value: number }[])[0]
          const item = validItems.find((i) => i.collected_at === p.axisValue)
          if (!item) return ''
          const value = cfg.value(item) as number
          return `${formatDateTime(item.collected_at)}<br/>${cfg.label}: ${cfg.format(value)}`
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
        name: cfg.unit,
        axisLabel: {
          formatter: (value: number) => (metric === 'written_tb' ? formatTbAxis(value) : formatNumber(value)),
        },
      },
      series: [
        {
          type: 'line',
          data: validItems.map((i) => cfg.value(i) as number),
          smooth: true,
          symbol: 'circle',
          symbolSize: 8,
          itemStyle: { color: cfg.color },
          lineStyle: { width: 3 },
          areaStyle: {
            color: {
              type: 'linear',
              x: 0,
              y: 0,
              x2: 0,
              y2: 1,
              colorStops: [
                { offset: 0, color: cfg.areaColor },
                { offset: 1, color: cfg.areaColor.replace(/0\.25\)/, '0.02)') },
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

  const validCount = items.filter((i) => METRICS[metric].value(i) != null).length

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
            { label: '寿命剩余 %', value: 'life_left_pct' },
            { label: '累计写入量', value: 'written_tb' },
            ...(hasReallocHistory ? [{ label: '重分配扇区', value: 'reallocated_ct' } as const] : []),
            ...(hasCrcHistory ? [{ label: 'UDMA CRC', value: 'udma_crc_errors' } as const] : []),
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
