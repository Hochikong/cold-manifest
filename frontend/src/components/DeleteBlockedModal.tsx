import { useNavigate } from 'react-router-dom'
import { Button, Modal, Space, Typography } from 'antd'
import type { DeleteRegistryBlocked } from '../api/client'

const { Text } = Typography

interface DeleteBlockedModalProps {
  blocked: DeleteRegistryBlocked | null
  onClose: () => void
}

/**
 * 删除磁盘 / 卷被快照阻塞（409）：展示原因 + 快照/对比清单（可点击跳转处理）。
 * 明确不做级联：不提供任何 force 选项。
 */
export default function DeleteBlockedModal({ blocked, onClose }: DeleteBlockedModalProps) {
  const navigate = useNavigate()
  return (
    <Modal
      open={!!blocked}
      title="无法删除：其下仍有快照"
      onCancel={onClose}
      width={560}
      footer={<Button type="primary" onClick={onClose}>知道了</Button>}
    >
      {blocked && (
        <Space orientation="vertical" size="middle" style={{ width: '100%' }}>
          <Text>{blocked.message}</Text>
          {blocked.snapshots.length > 0 && (
            <div>
              <Text strong>相关快照：</Text>
              <div style={{ marginTop: 4 }}>
                {blocked.snapshots.map((sid) => (
                  <div key={sid} style={{ padding: '2px 0' }}>
                    <Button
                      type="link"
                      style={{ padding: 0, height: 'auto', maxWidth: '100%' }}
                      onClick={() => {
                        onClose()
                        navigate(`/snapshots?snapshot=${encodeURIComponent(sid)}`)
                      }}
                    >
                      <Text code style={{ whiteSpace: 'normal', wordBreak: 'break-all' }}>{sid}</Text>
                    </Button>
                  </div>
                ))}
              </div>
            </div>
          )}
          {blocked.diffs.length > 0 && (
            <div>
              <Text strong>相关对比：</Text>
              <div style={{ marginTop: 4 }}>
                {blocked.diffs.map((id) => (
                  <div key={id} style={{ padding: '2px 0' }}>
                    <Button
                      type="link"
                      style={{ padding: 0, height: 'auto', maxWidth: '100%' }}
                      onClick={() => {
                        onClose()
                        navigate(`/diff?id=${encodeURIComponent(id)}`)
                      }}
                    >
                      <Text code style={{ whiteSpace: 'normal', wordBreak: 'break-all' }}>{id}</Text>
                    </Button>
                  </div>
                ))}
              </div>
            </div>
          )}
          <Text type="secondary">请先在快照页删除这些快照与相关对比，再回来删除。</Text>
        </Space>
      )}
    </Modal>
  )
}
