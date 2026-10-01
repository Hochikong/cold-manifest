import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Button, Input, Spin, Typography } from 'antd'
import type { InputRef } from 'antd'
import { FileOutlined, FolderOutlined, SearchOutlined } from '@ant-design/icons'
import { useGlobalSearch } from '../api/hooks'
import type { GlobalSearchItem } from '../api/client'
import { formatDateTime, formatFileSize } from '../utils/format'
import { dirNameOf, snapshotBrowseUrl } from '../utils/path'

const MIN_CHARS = 2
const DEBOUNCE_MS = 300
const DROPDOWN_LIMIT = 8

function shortSnapshotId(snapshot_id: string): string {
  const parts = snapshot_id.split('/')
  return parts.length > 1 ? parts[1] : snapshot_id
}

/** 点击命中行：文件 → 定位到所在目录；目录 → 打开该目录。 */
function goToEntry(navigate: ReturnType<typeof useNavigate>, item: GlobalSearchItem): void {
  const dir = item.type === 'dir' ? item.path : dirNameOf(item.path)
  navigate(snapshotBrowseUrl(item.snapshot_id, dir))
}

export default function GlobalSearch() {
  const navigate = useNavigate()
  const inputRef = useRef<InputRef>(null)
  const wrapRef = useRef<HTMLDivElement>(null)
  const [value, setValue] = useState('')
  const [focused, setFocused] = useState(false)
  const [debouncedQ, setDebouncedQ] = useState('')
  const [highlight, setHighlight] = useState(-1)

  // Ctrl/Cmd+K 聚焦
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        inputRef.current?.focus()
        inputRef.current?.select()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  // 输入防抖（300ms）；不足 2 字符不发请求
  useEffect(() => {
    const t = window.setTimeout(() => {
      const q = value.trim()
      setDebouncedQ(q.length >= MIN_CHARS ? q : '')
      setHighlight(-1)
    }, DEBOUNCE_MS)
    return () => window.clearTimeout(t)
  }, [value])

  const enabled = debouncedQ.length >= MIN_CHARS
  const { data, isFetching, error } = useGlobalSearch({ q: debouncedQ, limit: DROPDOWN_LIMIT }, enabled)
  const items = useMemo(() => data?.items ?? [], [data])
  const dropdownOpen = focused && enabled

  const closeAndBlur = () => {
    setFocused(false)
    inputRef.current?.blur()
  }

  const openAllResults = () => {
    closeAndBlur()
    navigate(`/search?q=${encodeURIComponent(debouncedQ)}`)
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Escape') {
      e.stopPropagation()
      closeAndBlur()
      return
    }
    if (!dropdownOpen) return
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setHighlight((h) => (items.length ? (h + 1) % items.length : -1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setHighlight((h) => (items.length ? (h <= 0 ? items.length - 1 : h - 1) : -1))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      if (highlight >= 0 && items[highlight]) {
        closeAndBlur()
        goToEntry(navigate, items[highlight])
      } else {
        openAllResults()
      }
    }
  }

  return (
    <div
      ref={wrapRef}
      style={{ position: 'relative' }}
      onBlur={(e) => {
        if (!wrapRef.current?.contains(e.relatedTarget as Node | null)) setFocused(false)
      }}
    >
      <Input
        ref={inputRef}
        placeholder="搜索文件 / 目录…"
        prefix={<SearchOutlined style={{ color: '#bfbfbf' }} />}
        suffix={
          !focused && !value ? (
            <Typography.Text type="secondary" style={{ fontSize: 12, userSelect: 'none' }}>
              Ctrl K
            </Typography.Text>
          ) : null
        }
        allowClear
        style={{ width: 280 }}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onFocus={() => setFocused(true)}
        onKeyDown={handleKeyDown}
        aria-label="全局搜索"
      />
      {dropdownOpen && (
        <div
          style={{
            position: 'absolute',
            top: 'calc(100% + 4px)',
            right: 0,
            width: 560,
            maxWidth: 'calc(100vw - 48px)',
            background: '#fff',
            borderRadius: 8,
            boxShadow: '0 6px 16px rgba(0, 0, 0, 0.12)',
            zIndex: 1100,
            overflow: 'hidden',
          }}
          onMouseDown={(e) => e.preventDefault()}
        >
          {isFetching && items.length === 0 && (
            <div style={{ padding: 24, textAlign: 'center' }}>
              <Spin />
            </div>
          )}
          {error && (
            <div style={{ padding: 16 }}>
              <Typography.Text type="danger">搜索失败，请稍后重试</Typography.Text>
            </div>
          )}
          {!error && !isFetching && items.length === 0 && (
            <div style={{ padding: 24, textAlign: 'center' }}>
              <Typography.Text type="secondary">无匹配结果</Typography.Text>
            </div>
          )}
          {items.map((item, idx) => (
            <div
              key={`${item.snapshot_id}:${item.entry_id}`}
              onClick={() => {
                closeAndBlur()
                goToEntry(navigate, item)
              }}
              style={{
                display: 'flex',
                alignItems: 'flex-start',
                gap: 10,
                padding: '8px 16px',
                cursor: 'pointer',
                background: idx === highlight ? 'rgba(0, 0, 0, 0.04)' : undefined,
              }}
              onMouseEnter={() => setHighlight(idx)}
            >
              <span style={{ color: '#8c8c8c', marginTop: 2, flexShrink: 0 }}>
                {item.type === 'dir' ? <FolderOutlined /> : <FileOutlined />}
              </span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <Typography.Text strong ellipsis style={{ display: 'block' }}>
                  {item.name}
                </Typography.Text>
                <Typography.Text type="secondary" ellipsis style={{ display: 'block', fontSize: 12 }}>
                  {item.path}
                </Typography.Text>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {shortSnapshotId(item.snapshot_id)} · {formatDateTime(item.collected_at)}
                  {item.type === 'file' && item.size_bytes != null ? ` · ${formatFileSize(item.size_bytes)}` : ''}
                </Typography.Text>
              </div>
            </div>
          ))}
          {(items.length > 0 || data?.has_more) && (
            <div
              style={{
                borderTop: '1px solid #f0f0f0',
                padding: '6px 16px',
                textAlign: 'center',
              }}
            >
              <Button type="link" size="small" onClick={openAllResults}>
                查看全部结果{data?.has_more ? '（还有更多）' : `（共 ${items.length} 条）`}
              </Button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
