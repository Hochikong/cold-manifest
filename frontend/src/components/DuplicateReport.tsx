import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Col,
  Empty,
  Row,
  Segmented,
  Skeleton,
  Space,
  Statistic,
  Table,
  Typography,
} from 'antd'
import { FileTextOutlined } from '@ant-design/icons'
import ErrorAlert from './ErrorAlert'
import { formatFileSize, formatNumber } from '../utils/format'
import { getDuplicates, type DuplicateItem, type DuplicatesResponse } from '../api/client'
import { useSettings } from '../api/hooks'

const { Text } = Typography

const MIN_SIZE_OPTIONS = [
  { label: '0 B', value: 0 },
  { label: '1 MiB', value: 1024 * 1024 },
  { label: '10 MiB', value: 10 * 1024 * 1024 },
  { label: '100 MiB', value: 100 * 1024 * 1024 },
]

const PAGE_SIZE = 100

function isPolicyError(error: unknown): boolean {
  if (!error || typeof error !== 'object') return false
  const e = error as { response?: { data?: { detail?: string } }; message?: string }
  const detail = e.response?.data?.detail || e.message || ''
  return /hash_policy.*full|完整哈希|full.*hash|hash.*full/i.test(detail)
}

export default function DuplicateReport({ snapshotId }: { snapshotId: string }) {
  const { data: settings } = useSettings()
  const [minSize, setMinSize] = useState<number>(1024 * 1024)
  const [data, setData] = useState<DuplicatesResponse | null>(null)
  const [allItems, setAllItems] = useState<DuplicateItem[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState<unknown>(null)

  const params = useMemo(
    () => ({ min_size: minSize, limit: PAGE_SIZE }),
    [minSize]
  )

  useEffect(() => {
    let cancelled = false
    getDuplicates(snapshotId, params)
      .then((res) => {
        if (cancelled) return
        setData(res)
        setAllItems(res.items)
        setCursor(res.next_cursor)
      })
      .catch((e) => {
        if (cancelled) return
        setError(e)
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [snapshotId, params])

  const handleMinSizeChange = (value: number) => {
    setMinSize(value)
    setIsLoading(true)
    setData(null)
    setAllItems([])
    setCursor(null)
    setError(null)
  }

  const loadMore = async () => {
    if (!cursor) return
    setLoadingMore(true)
    try {
      const res = await getDuplicates(snapshotId, { ...params, cursor })
      setData(res)
      setAllItems((prev) => [...prev, ...res.items])
      setCursor(res.next_cursor)
    } finally {
      setLoadingMore(false)
    }
  }

  if (error && isPolicyError(error)) {
    const detail =
      (error as { response?: { data?: { detail?: string } } }).response?.data?.detail ||
      '该快照未使用完整哈希策略，无法生成重复文件报告。'
    const dataRoot = settings?.data_root || ''
    const hashCommand = `cldm hash ${snapshotId} --policy full${dataRoot ? ` --data-root "${dataRoot}"` : ''}`
    return (
      <div>
        <Card title="重复文件" size="small" style={{ marginBottom: 16 }}>
          <Space orientation="vertical" size="middle" style={{ width: '100%' }}>
            <Alert
              type="warning"
              showIcon
              title="需要先计算完整哈希"
              description={detail}
            />
            <Text>
              可执行命令启用完整哈希后重新查看：
            </Text>
            <Text code>
              {hashCommand}
            </Text>
          </Space>
        </Card>
      </div>
    )
  }

  if (error) {
    return <ErrorAlert error={error} />
  }

  const showSkeleton = isLoading && allItems.length === 0

  return (
    <div>
      <Card title="重复文件" size="small" style={{ marginBottom: 16 }}>
        <Space orientation="vertical" size="middle" style={{ width: '100%' }}>
          <Text type="secondary">
            仅统计已计算完整哈希的文件；重复组按浪费空间从大到小排列。
          </Text>
          <Space wrap>
            <Text>最小文件大小：</Text>
            <Segmented<number>
              value={minSize}
              onChange={handleMinSizeChange}
              options={MIN_SIZE_OPTIONS}
            />
          </Space>
        </Space>
      </Card>

      {showSkeleton ? (
        <Card size="small">
          <Skeleton active paragraph={{ rows: 6 }} />
        </Card>
      ) : (
        <>
          <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
            <Col xs={24} sm={8}>
              <Card size="small">
                <Statistic
                  title="重复组数"
                  value={data?.duplicate_groups ?? 0}
                  suffix="组"
                />
              </Card>
            </Col>
            <Col xs={24} sm={8}>
              <Card size="small">
                <Statistic
                  title="可回收空间"
                  value={formatFileSize(data?.total_wasted_bytes ?? 0)}
                />
              </Card>
            </Col>
            <Col xs={24} sm={8}>
              <Card size="small">
                <Statistic
                  title="已哈希文件数"
                  value={data?.hashed_files ?? 0}
                  suffix="个"
                />
              </Card>
            </Col>
          </Row>

          <Card size="small">
            {allItems.length ? (
              <>
                <Table<DuplicateItem>
                  rowKey="hash_hex"
                  size="small"
                  pagination={false}
                  scroll={{ x: 'max-content' }}
                  expandable={{
                    expandedRowRender: (record) => (
                      <Space orientation="vertical" style={{ width: '100%' }}>
                        {record.paths.map((p, idx) => (
                          <Text key={idx}>
                            <FileTextOutlined style={{ marginRight: 8 }} />
                            {p}
                          </Text>
                        ))}
                        {record.paths_truncated && (
                          <Text type="warning">仅列出前 20 条路径，完整清单请用 CLI 导出 CSV。</Text>
                        )}
                      </Space>
                    ),
                    rowExpandable: () => true,
                  }}
                  columns={[
                    {
                      title: '文件大小',
                      dataIndex: 'size_bytes',
                      width: 140,
                      render: (v: number) => formatFileSize(v),
                    },
                    {
                      title: '重复份数',
                      dataIndex: 'count',
                      width: 110,
                      render: (v: number) => formatNumber(v),
                    },
                    {
                      title: '浪费空间',
                      dataIndex: 'wasted_bytes',
                      width: 140,
                      render: (v: number) => formatFileSize(v),
                    },
                    {
                      title: '哈希',
                      dataIndex: 'hash_hex',
                      ellipsis: true,
                      render: (v: string) => <Text code>{v}</Text>,
                    },
                    {
                      title: '路径摘要',
                      key: 'paths',
                      ellipsis: true,
                      render: (_: unknown, record: DuplicateItem) => {
                        const first = record.paths[0] || '-'
                        const rest = record.count - 1
                        return (
                          <Text>
                            {first}
                            {rest > 0 ? (
                              <Text type="secondary"> 等 {formatNumber(rest)} 条</Text>
                            ) : null}
                            {record.paths_truncated ? '（仅列部分）' : null}
                          </Text>
                        )
                      },
                    },
                  ]}
                  dataSource={allItems}
                />
                {cursor && (
                  <div style={{ textAlign: 'center', marginTop: 16 }}>
                    <Button loading={loadingMore || isLoading} onClick={loadMore}>
                      加载更多
                    </Button>
                  </div>
                )}
              </>
            ) : (
              <Empty description={`没有 ≥ ${formatFileSize(minSize)} 的重复文件`} />
            )}
          </Card>
        </>
      )}
    </div>
  )
}
