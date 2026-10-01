import { useCallback, useState } from 'react'
import RowContextMenuOverlay from '../components/RowContextMenuOverlay'
import type { RowContextMenuState, RowContextTarget } from '../components/RowContextMenuOverlay'

/**
 * 表格 / 列表行的右键菜单：
 *   const menu = useRowContextMenu()
 *   <Table onRow={(r) => ({ onContextMenu: (e) => menu.open(e, { path: r.path, snapshotId }) })} />
 *   {menu.element}
 */
export function useRowContextMenu() {
  const [state, setState] = useState<RowContextMenuState | null>(null)
  const open = useCallback((e: React.MouseEvent, target: RowContextTarget) => {
    e.preventDefault()
    e.stopPropagation()
    setState({ x: e.clientX, y: e.clientY, target })
  }, [])
  const element = <RowContextMenuOverlay state={state} onClose={() => setState(null)} />
  return { open, element }
}

export type { RowContextTarget }
