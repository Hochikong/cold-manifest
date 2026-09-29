export function formatTaskMessage(message: string | null): string {
  if (!message) return '准备中'

  if (message.startsWith('scan:')) {
    const payload = message.slice('scan:'.length)
    const [done] = payload.split('/')
    if (done !== undefined) {
      return `已扫描 ${Number(done).toLocaleString('zh-CN')} 条`
    }
  }

  if (message.startsWith('parse:')) {
    const payload = message.slice('parse:'.length)
    const [done] = payload.split('/')
    if (done !== undefined) {
      return `已解析 ${Number(done).toLocaleString('zh-CN')} 行`
    }
  }

  const m = message.match(/^(\w+):(\d+)\/(\d+)$/)
  if (m) {
    const [, phase, done, total] = m
    const phaseName: Record<string, string> = {
      probe: '探测',
      scan: '扫描',
      seal: '封存',
      copy: '复制',
      register: '注册',
      done: '完成',
      index: '索引',
      rollup: '汇总',
      optimize: '优化',
    }
    return `${phaseName[phase] ?? phase} ${done}/${total}`
  }

  return message
}

export function formatTaskStatus(status: string): string {
  const map: Record<string, string> = {
    pending: '待处理',
    running: '运行中',
    cancelling: '取消中',
    cancelled: '已取消',
    done: '完成',
    error: '失败',
  }
  return map[status] ?? status
}
