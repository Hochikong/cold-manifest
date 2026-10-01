import { App } from 'antd'
import { apiErrorDetail } from '../api/client'

/**
 * 提交失败统一提示：优先展示后端返回的 detail 原文（支持字符串与 {message,...} 对象），
 * 兜底 axios 错误信息。用 notification 且不自动消失——用户能看清原因、可复制，再手动关闭。
 */
export function useShowApiError() {
  const { notification } = App.useApp()
  return (err: unknown, title = '操作失败') => {
    notification.error({
      title,
      description: apiErrorDetail(err),
      placement: 'top',
      duration: 0,
    })
  }
}
