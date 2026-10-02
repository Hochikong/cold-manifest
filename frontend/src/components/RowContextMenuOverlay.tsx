import { useEffect } from 'react'
import { App, Dropdown } from 'antd'
import type { MenuProps } from 'antd'
import { CopyOutlined, FolderOpenOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { baseNameOf, dirNameOf, snapshotBrowseUrl } from '../utils/path'

/** 「打开所在快照浏览」单侧打开项（对比行等多侧场景）。 */
export interface RowContextOpenItem {
  snapshotId: string
  /** 定位到的目录 */
  dir: string
  /** 菜单文案，如「在 A · 移动盘 中浏览」 */
  label: string
}

/** 行级右键菜单目标：至少要有完整路径；知道所属快照才启用「打开所在快照浏览」。 */
export interface RowContextTarget {
  /** 完整路径（含名称） */
  path: string
  /** 名称；缺省取 path 末段 */
  name?: string
  /** 所属快照 ID */
  snapshotId?: string
  /** 「打开所在快照浏览」定位到的目录；缺省取 path 的父目录（目录行通常传自身） */
  openDirPath?: string
  /** 多侧打开项；提供后优先于 snapshotId 单项（单项时直接平铺，多项时收进子菜单） */
  extraOpenItems?: RowContextOpenItem[]
}

export interface RowContextMenuState {
  x: number
  y: number
  target: RowContextTarget
}

async function copyText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text)
    return
  }
  // 非安全上下文回退（老浏览器 / http 部署）
  const ta = document.createElement('textarea')
  ta.value = text
  ta.style.position = 'fixed'
  ta.style.opacity = '0'
  document.body.appendChild(ta)
  ta.select()
  try {
    document.execCommand('copy')
  } finally {
    ta.remove()
  }
}

/** 固定定位在 (x, y) 的菜单浮层；由 useRowContextMenu 渲染。 */
export default function RowContextMenuOverlay({ state, onClose }: { state: RowContextMenuState | null; onClose: () => void }) {
  const { message } = App.useApp()
  const navigate = useNavigate()

  // Esc / 滚动 / 窗口缩放时关闭（菜单钉在打开时的坐标，不跟手）
  useEffect(() => {
    if (!state) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    window.addEventListener('scroll', onClose, true)
    window.addEventListener('resize', onClose)
    return () => {
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('scroll', onClose, true)
      window.removeEventListener('resize', onClose)
    }
  }, [state, onClose])

  if (!state) return null
  const { x, y, target } = state
  const name = target.name ?? baseNameOf(target.path)

  const doCopy = async (text: string, label: string) => {
    try {
      await copyText(text)
      message.success(`已复制${label}`)
    } catch {
      message.error('复制失败，请手动选择文本复制')
    }
  }

  const openItems: RowContextOpenItem[] = target.extraOpenItems?.length
    ? target.extraOpenItems
    : target.snapshotId
      ? [{ snapshotId: target.snapshotId, dir: target.openDirPath ?? dirNameOf(target.path), label: '' }]
      : []

  const items: MenuProps['items'] = [
    {
      key: 'copy-path',
      icon: <CopyOutlined />,
      label: '复制路径',
      onClick: () => {
        void doCopy(target.path, '路径')
        onClose()
      },
    },
    {
      key: 'copy-name',
      icon: <CopyOutlined />,
      label: '复制文件名',
      onClick: () => {
        void doCopy(name, '文件名')
        onClose()
      },
    },
  ]
  if (openItems.length === 1) {
    items.push(
      { type: 'divider' },
      {
        key: 'open-in-snapshot',
        icon: <FolderOpenOutlined />,
        label: openItems[0].label || '打开所在快照浏览',
        onClick: () => {
          navigate(snapshotBrowseUrl(openItems[0].snapshotId, openItems[0].dir))
          onClose()
        },
      },
    )
  } else if (openItems.length > 1) {
    items.push(
      { type: 'divider' },
      {
        key: 'open-in-snapshot',
        icon: <FolderOpenOutlined />,
        label: '打开所在快照浏览',
        children: openItems.map((it, i) => ({
          key: `open-in-snapshot-${i}`,
          label: it.label || '打开所在快照浏览',
          onClick: () => {
            navigate(snapshotBrowseUrl(it.snapshotId, it.dir))
            onClose()
          },
        })),
      },
    )
  }

  return (
    <div style={{ position: 'fixed', left: x, top: y, width: 0, height: 0, zIndex: 1050 }}>
      <Dropdown open menu={{ items }} onOpenChange={(o) => { if (!o) onClose() }}>
        <span />
      </Dropdown>
    </div>
  )
}
