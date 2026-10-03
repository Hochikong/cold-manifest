import { Tag, Tooltip } from 'antd'
import { CheckCircleOutlined, ExclamationCircleOutlined, QuestionCircleOutlined, StopOutlined } from '@ant-design/icons'

/** 后端磁盘身份状态：verified=已验证 / unverified=未验证 / conflict=冲突 / unknown=未知。 */
export type IdentityStatus = 'verified' | 'unverified' | 'conflict' | 'unknown'

interface IdentityStatusInfo {
  label: string
  color: string
  icon: React.ReactNode
}

const STATUS_META: Record<IdentityStatus, IdentityStatusInfo> = {
  verified: { label: '已验证', color: 'success', icon: <CheckCircleOutlined /> },
  unverified: { label: '未验证', color: 'warning', icon: <ExclamationCircleOutlined /> },
  conflict: { label: '身份冲突', color: 'error', icon: <StopOutlined /> },
  unknown: { label: '未知', color: 'default', icon: <QuestionCircleOutlined /> },
}

function normalizeIdentityStatus(v: string | null | undefined): IdentityStatus {
  return v === 'verified' || v === 'unverified' || v === 'conflict' ? v : 'unknown'
}

/**
 * 磁盘身份徽标。reason（identity_status_reason）存在时挂 Tooltip；
 * 不传 reason 可强制隐藏 Tooltip（如徽标旁已单独展示原因文字）。
 */
export default function IdentityStatusBadge({
  status,
  reason,
  withTooltip = true,
}: {
  status: string | null | undefined
  reason?: string | null
  /** 设 false 时即使有 reason 也不显示 Tooltip（原因文字在别处展示的场景） */
  withTooltip?: boolean
}) {
  const meta = STATUS_META[normalizeIdentityStatus(status)]
  const tip = withTooltip && reason ? reason : null
  const tag = (
    <Tag color={meta.color} icon={meta.icon} style={{ marginInlineEnd: 0 }}>
      {meta.label}
    </Tag>
  )
  return tip ? <Tooltip title={tip}>{tag}</Tooltip> : tag
}
