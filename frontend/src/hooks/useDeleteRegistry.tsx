import { useState } from 'react'
import { App } from 'antd'
import axios from 'axios'
import { apiErrorDetail, deleteDisk, deleteVolume, type DeleteRegistryBlocked } from '../api/client'
import DeleteBlockedModal from '../components/DeleteBlockedModal'

export type DeleteTarget =
  | { kind: 'disk'; id: string; label: string }
  | { kind: 'volume'; id: string; label: string }

/**
 * 磁盘 / 卷登记记录删除的统一入口：先弹确认，再调 DELETE；
 * 409（仍有快照）时展示阻塞清单（不提供 force / 级联），成功后 toast + 失效缓存。
 * onSuccess 在删除成功后回调（关抽屉 / 回列表等）。
 */
export function useDeleteRegistry() {
  const { modal, message } = App.useApp()
  const [blocked, setBlocked] = useState<DeleteRegistryBlocked | null>(null)

  const run = async (target: DeleteTarget, onSuccess: () => void) => {
    try {
      const res = target.kind === 'disk' ? await deleteDisk(target.id) : await deleteVolume(target.id)
      message.success(
        `已删除「${target.label}」的登记记录 · 只删除登记记录，磁盘上的数据与盘上副本不受影响` +
          (res.deleted_volumes ? `（连同 ${res.deleted_volumes} 个卷的登记）` : ''),
      )
      onSuccess()
    } catch (e) {
      if (axios.isAxiosError(e) && e.response?.status === 409) {
        // 409 响应体形如 {"detail": {message, snapshots, diffs}}；detail 为嵌套对象
        const detail = (e.response.data as { detail?: unknown } | undefined)?.detail
        if (detail && typeof detail === 'object' && 'message' in (detail as Record<string, unknown>)) {
          setBlocked(detail as DeleteRegistryBlocked)
        } else {
          message.error(typeof detail === 'string' && detail ? detail : '删除被阻塞：其下仍有快照')
        }
      } else {
        message.error(apiErrorDetail(e) || '删除失败')
      }
    }
  }

  const confirmDelete = (target: DeleteTarget, onSuccess: () => void = () => {}) => {
    modal.confirm({
      title: `删除${target.kind === 'disk' ? '磁盘' : '卷'}「${target.label}」的登记记录？`,
      content: '只删除登记记录，不删除磁盘 / 卷上的任何数据与盘上副本。若其下仍有快照，将无法删除。',
      okText: '删除登记',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: () => run(target, onSuccess),
    })
  }

  const modalNode = <DeleteBlockedModal blocked={blocked} onClose={() => setBlocked(null)} />
  return { confirmDelete, modalNode }
}
