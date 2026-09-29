import { Alert } from 'antd'

interface ErrorAlertProps {
  error: unknown
}

export default function ErrorAlert({ error }: ErrorAlertProps) {
  const message = error instanceof Error ? error.message : String(error) || '未知错误'
  return <Alert type="error" showIcon title={message} style={{ marginBottom: 16 }} />
}
