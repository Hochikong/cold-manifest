import { useCallback, useEffect, useMemo, useRef, useSyncExternalStore } from 'react'
import { App, Button } from 'antd'
import { useNavigate } from 'react-router-dom'
import { useTasks } from '../api/hooks'
import type { Task } from '../api/client'
import { getTaskSummary, isTerminalStatus, statusLabel, taskTypeLabel } from '../utils/taskNotification'

const NOTIFIED_KEY = 'cldm_task_notified_v1'
const READ_KEY = 'cldm_task_read_v1'

function loadSet(key: string): Set<string> {
  try {
    const raw = localStorage.getItem(key)
    if (raw) {
      const obj = JSON.parse(raw) as Record<string, unknown>
      return new Set(Object.keys(obj))
    }
  } catch {
    // ignore corrupted storage
  }
  return new Set()
}

function saveSet(key: string, set: Set<string>) {
  try {
    const obj: Record<string, string> = {}
    set.forEach((id) => {
      obj[id] = '1'
    })
    localStorage.setItem(key, JSON.stringify(obj))
  } catch {
    // ignore storage errors
  }
}

function addAll(prev: Set<string>, ids: string[]): Set<string> {
  if (ids.length === 0) return prev
  const next = new Set(prev)
  ids.forEach((id) => next.add(id))
  return next
}

class TaskNotifyStore {
  notified = loadSet(NOTIFIED_KEY)
  read = loadSet(READ_KEY)
  private snapshot: readonly [Set<string>, Set<string>]
  private listeners = new Set<() => void>()

  constructor() {
    this.snapshot = [this.notified, this.read]
  }

  subscribe(listener: () => void) {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  private emit() {
    this.snapshot = [this.notified, this.read]
    this.listeners.forEach((listener) => listener())
  }

  getSnapshot() {
    return this.snapshot
  }

  markNotified(ids: string[]) {
    const next = addAll(this.notified, ids)
    if (next === this.notified) return
    this.notified = next
    saveSet(NOTIFIED_KEY, next)
    this.emit()
  }

  markRead(ids: string[]) {
    const next = addAll(this.read, ids)
    if (next === this.read) return
    this.read = next
    saveSet(READ_KEY, next)
    this.emit()
  }
}

const taskNotifyStore = new TaskNotifyStore()

export interface TaskNotification {
  task: Task
  isUnread: boolean
}

export function useTaskNotifications() {
  const { notification } = App.useApp()
  const navigate = useNavigate()
  const { data } = useTasks({ limit: 20 }, true)
  const [notified, read] = useSyncExternalStore(
    useCallback((callback) => taskNotifyStore.subscribe(callback), []),
    () => taskNotifyStore.getSnapshot()
  )
  const firstLoadRef = useRef(false)

  const terminalTasks = useMemo(() => {
    return (data?.items ?? []).filter((task) => isTerminalStatus(task.status))
  }, [data])

  const showNotification = useCallback(
    (task: Task) => {
      const typeLabel = taskTypeLabel(task.type)
      const statusText = statusLabel(task.status)
      const summary = getTaskSummary(task)
      const result = task.result ?? {}
      const snapshotId = typeof result.snapshot_id === 'string' ? result.snapshot_id : undefined
      const diffId = typeof result.diff_id === 'string' ? result.diff_id : undefined

      const close = () => notification.destroy(`task-${task.id}`)

      notification.open({
        key: `task-${task.id}`,
        title: `${typeLabel}${statusText}`,
        description: summary,
        duration: 0,
        placement: 'topRight',
        actions: [
          <Button
            key="tasks"
            type="primary"
            size="small"
            onClick={() => {
              close()
              navigate('/tasks')
            }}
          >
            查看任务
          </Button>,
          snapshotId ? (
            <Button
              key="snapshot"
              size="small"
              onClick={() => {
                close()
                navigate(`/snapshots?snapshot=${encodeURIComponent(snapshotId)}`)
              }}
            >
              查看快照
            </Button>
          ) : null,
          diffId ? (
            <Button
              key="diff"
              size="small"
              onClick={() => {
                close()
                navigate(`/diff?id=${encodeURIComponent(diffId)}`)
              }}
            >
              查看对比
            </Button>
          ) : null,
        ].filter(Boolean),
      })
    },
    [notification, navigate]
  )

  useEffect(() => {
    if (!data) return

    const terminalIds = terminalTasks.map((task) => task.id)

    if (!firstLoadRef.current) {
      taskNotifyStore.markNotified(terminalIds)
      taskNotifyStore.markRead(terminalIds)
      firstLoadRef.current = true
      return
    }

    const toNotify = terminalTasks.filter((task) => !notified.has(task.id))
    if (toNotify.length > 0) {
      taskNotifyStore.markNotified(toNotify.map((task) => task.id))
      toNotify.forEach((task) => showNotification(task))
    }
  }, [data, terminalTasks, notified, showNotification])

  const notifications = useMemo(() => {
    return terminalTasks.map((task) => ({
      task,
      isUnread: !read.has(task.id),
    }))
  }, [terminalTasks, read])

  const unreadCount = useMemo(() => {
    return terminalTasks.filter((task) => !read.has(task.id)).length
  }, [terminalTasks, read])

  const markAllRead = useCallback(() => {
    taskNotifyStore.markRead(terminalTasks.map((task) => task.id))
  }, [terminalTasks])

  return { notifications, unreadCount, markAllRead }
}
