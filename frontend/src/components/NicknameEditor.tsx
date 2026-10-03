import { useEffect, useRef, useState } from 'react'
import { Button, Input, Space, Typography } from 'antd'
import type { InputRef } from 'antd'
import { EditOutlined } from '@ant-design/icons'

const { Text } = Typography

interface NicknameEditorProps {
  value: string | null
  /** 保存（空串 = 清除）；失败时抛错由调用方提示，编辑框直接收起 */
  onSave: (nickname: string) => Promise<unknown>
  /** 未设置时的占位文案 */
  placeholder?: string
  /** 编辑框宽度（表格内建议 200 左右） */
  inputWidth?: number
  maxLength?: number
  /** 无障碍标签 / 编辑按钮 title（默认按昵称） */
  ariaLabel?: string
}

/**
 * 昵称行内编辑：点击铅笔进入编辑，Enter 保存 / Esc 取消 / 失焦保存，
 * 清空后保存即清除昵称。展示态：有昵称加粗，无昵称灰色占位。
 */
export default function NicknameEditor({
  value,
  onSave,
  placeholder = '未设置',
  inputWidth = 200,
  maxLength = 64,
  ariaLabel = '昵称',
}: NicknameEditorProps) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [saving, setSaving] = useState(false)
  const inputRef = useRef<InputRef>(null)

  useEffect(() => {
    if (editing) inputRef.current?.focus({ cursor: 'end' })
  }, [editing])

  const start = () => {
    setDraft(value ?? '')
    setEditing(true)
  }

  const cancel = () => setEditing(false)

  const save = async () => {
    if (saving) return
    const next = draft.trim()
    if (next === (value ?? '').trim()) {
      setEditing(false)
      return
    }
    setSaving(true)
    try {
      await onSave(next)
      setEditing(false)
    } catch {
      setEditing(false)
    } finally {
      setSaving(false)
    }
  }

  if (editing) {
    return (
      <Input
        ref={inputRef}
        size="small"
        style={{ width: inputWidth, maxWidth: '100%' }}
        maxLength={maxLength}
        value={draft}
        placeholder="留空并回车清除"
        aria-label={ariaLabel}
        disabled={saving}
        onChange={(e) => setDraft(e.target.value)}
        onPressEnter={() => void save()}
        onKeyDown={(e) => {
          if (e.key === 'Escape') cancel()
        }}
        onBlur={() => void save()}
      />
    )
  }

  return (
    <Space size={4} style={{ maxWidth: '100%' }}>
      {value ? (
        <Text strong style={{ maxWidth: inputWidth - 28, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', display: 'inline-block', verticalAlign: 'bottom' }}>
          {value}
        </Text>
      ) : (
        <Text type="secondary">{placeholder}</Text>
      )}
      <Button
        type="text"
        size="small"
        aria-label={`编辑${ariaLabel}`}
        title={`编辑${ariaLabel}`}
        icon={<EditOutlined style={{ color: value ? undefined : '#bfbfbf' }} />}
        onClick={start}
      />
    </Space>
  )
}
