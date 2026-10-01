import { Space, Tag, Tooltip, Typography } from 'antd'
import { CheckCircleOutlined, CloseCircleOutlined, ExclamationCircleOutlined, QuestionCircleOutlined } from '@ant-design/icons'
import type { OnDiskCopy } from '../api/client'
import { formatDateTime } from '../utils/format'

const { Text } = Typography

const STATUS_META: Record<string, { color: string; label: string; icon: React.ReactNode; hint: string }> = {
  ok: { color: 'success', label: '一致', icon: <CheckCircleOutlined />, hint: '副本已在采集时写入并校验' },
  missing: { color: 'error', label: '缺失', icon: <CloseCircleOutlined />, hint: '登记的副本文件在盘上已不存在' },
  stale: { color: 'warning', label: '不一致', icon: <ExclamationCircleOutlined />, hint: '副本与主机快照库不一致（过期）' },
  skipped_no_space: { color: 'warning', label: '未写入', icon: <ExclamationCircleOutlined />, hint: '采集时因磁盘空间不足未写入副本' },
}

/**
 * 盘上副本状态（只展示）：catalog.on_disk_copies 行 → 状态徽标 +
 * sha256 前 12 位 + 写入时间。校验按钮等后端接口就绪后再接入。
 */
export default function OnDiskCopyBadge({ copy }: { copy: OnDiskCopy | null }) {
  if (!copy) {
    return (
      <Space size={8} wrap>
        <Tag icon={<QuestionCircleOutlined />}>无记录</Tag>
        <Text type="secondary">该快照没有盘上副本登记（采集时未开启或为旧导入）</Text>
      </Space>
    )
  }

  const meta = STATUS_META[copy.status] ?? {
    color: 'default',
    label: copy.status,
    icon: <QuestionCircleOutlined />,
    hint: '未知状态',
  }

  return (
    <Space size={8} wrap>
      <Tooltip title={meta.hint}>
        <Tag color={meta.color} icon={meta.icon}>
          {meta.label}
        </Tag>
      </Tooltip>
      {copy.sha256 && (
        <Tooltip title={`完整 sha256：${copy.sha256}`}>
          <Text code copyable={{ text: copy.sha256 }} style={{ fontSize: 12 }}>
            {copy.sha256.slice(0, 12)}
          </Text>
        </Tooltip>
      )}
      {copy.copied_at && (
        <Text type="secondary" style={{ fontSize: 12 }}>
          写入于 {formatDateTime(copy.copied_at)}
        </Text>
      )}
    </Space>
  )
}
