import { useCallback, useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { App } from 'antd'

/** localStorage key 前缀：cldm_pagesize:<tableId> */
const PAGE_SIZE_KEY_PREFIX = 'cldm_pagesize:'
/** URL 上表示当前页（1 起）的查询参数名 */
export const PAGE_URL_PARAM = 'page'
/** 深链恢复的最大页数：超过则提示回到第 1 页，避免沿 cursor 打爆请求 */
export const MAX_URL_RESTORE_PAGE = 20

function loadPersistedPageSize(tableId: string, fallback: number): number {
  try {
    const raw = window.localStorage.getItem(PAGE_SIZE_KEY_PREFIX + tableId)
    const n = raw == null ? NaN : Number(raw)
    return Number.isInteger(n) && n > 0 ? n : fallback
  } catch {
    return fallback
  }
}

function persistPageSize(tableId: string, n: number) {
  try {
    window.localStorage.setItem(PAGE_SIZE_KEY_PREFIX + tableId, String(n))
  } catch {
    // localStorage 不可用（隐私模式等）时静默降级为不记忆
  }
}

/** 深链恢复沿 cursor 走页时只需要的字段 */
export type CursorPageResult = { next_cursor?: string | null; has_more?: boolean }

export interface UseCursorPagingOptions {
  /** 稳定的表标识：决定 localStorage 的行数记忆 key（cldm_pagesize:<tableId>） */
  tableId: string
  defaultPageSize?: number
  /**
   * 深链/刷新恢复第 N 页时，从第 1 页沿 next_cursor 走 N-1 步所用的一次取页函数。
   * 只在挂载时使用；不传则不恢复页码（只同步 URL）。
   */
  fetchPage?: (limit: number, cursor?: string) => Promise<CursorPageResult>
}

/**
 * cursor 栈分页的共享状态 hook：
 * - 行数按 tableId 持久化到 localStorage（默认 defaultPageSize），切换即时写入；
 * - 当前页写进 URL 的 page 查询参数（翻页/改行数/筛选重置都通过 history.replace 同步，
 *   保持其它查询参数不变）；page===1 时从 URL 移除该参数；
 * - 深链/刷新带 page=N 时，从第 1 页沿 next_cursor 自动走 N-1 步恢复（走的过程
 *   restoring=true，翻页按钮禁用，禁止重复触发）；页码 > MAX_URL_RESTORE_PAGE 时
 *   提示回到第 1 页而不是把请求打爆。
 */
export function useCursorPaging({ tableId, defaultPageSize = 50, fetchPage }: UseCursorPagingOptions) {
  const [searchParams, setSearchParams] = useSearchParams()
  const { message } = App.useApp()

  const [pageSize, setPageSizeState] = useState(() => loadPersistedPageSize(tableId, defaultPageSize))
  const [cursorStack, setCursorStack] = useState<(string | null)[]>([null])
  // 惰性初始化：挂载首次渲染就知道"正在恢复"，URL 同步 effect 不会抢在恢复前把 page 抹掉
  const [restoring, setRestoring] = useState(() => {
    const n = Number(new URLSearchParams(window.location.search).get(PAGE_URL_PARAM))
    return Number.isInteger(n) && n > 1
  })

  const pageIndex = cursorStack.length - 1
  const cursor = cursorStack[pageIndex] ?? undefined

  // 深链恢复只在挂载时做一次；fetchPage/pageSize 走 ref，避免回调身份变化重复触发
  const fetchPageRef = useRef(fetchPage)
  fetchPageRef.current = fetchPage
  const pageSizeRef = useRef(pageSize)
  pageSizeRef.current = pageSize

  useEffect(() => {
    const n = Number(new URLSearchParams(window.location.search).get(PAGE_URL_PARAM))
    const walk = fetchPageRef.current
    if (!walk || !Number.isInteger(n) || n <= 1) {
      // 无需恢复（含 StrictMode 二次挂载后 page 已被消费的场景）
      void Promise.resolve().then(() => setRestoring(false))
      return
    }
    if (n > MAX_URL_RESTORE_PAGE) {
      message.info(`页码过深（第 ${n} 页），已回到第 1 页；可用「下一页」逐步翻到目标页`)
      void Promise.resolve().then(() => setRestoring(false))
      return
    }
    let cancelled = false
    setRestoring(true)
    void (async () => {
      const stack: (string | null)[] = [null]
      let cur: string | undefined = undefined
      for (let i = 1; i < n; i++) {
        try {
          const res = await walk(pageSizeRef.current, cur)
          const nc = res.next_cursor
          if (!nc) break
          stack.push(nc)
          cur = nc
        } catch {
          break // 沿途取页失败就停在能到达的最后一页
        }
        if (cancelled) return
      }
      if (!cancelled) {
        setCursorStack(stack)
        setRestoring(false)
      }
    })()
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // URL page 参数与 pageIndex 保持同步（history.replace，不打新历史记录）
  useEffect(() => {
    if (restoring) return // 恢复期间栈还是第 1 页，别把深链的 page 抹掉
    const raw = searchParams.get(PAGE_URL_PARAM)
    const want = pageIndex > 0 ? String(pageIndex + 1) : null
    if (raw === want) return
    const next = new URLSearchParams(searchParams)
    if (want) next.set(PAGE_URL_PARAM, want)
    else next.delete(PAGE_URL_PARAM)
    setSearchParams(next, { replace: true })
  }, [pageIndex, restoring, searchParams, setSearchParams])

  /** 下一页：把 next_cursor 压栈（调用方负责判断 has_more） */
  const goNext = useCallback(
    (nextCursor: string | null | undefined) => {
      if (nextCursor) setCursorStack((s) => [...s, nextCursor])
    },
    [],
  )

  /** 上一页：弹栈 */
  const goPrev = useCallback(() => {
    setCursorStack((s) => (s.length > 1 ? s.slice(0, -1) : s))
  }, [])

  /** 筛选/排序变化等场景：回到第 1 页（URL 的 page 由同步 effect 清掉） */
  const resetPage = useCallback(() => {
    setCursorStack([null])
  }, [])

  /** 改每页行数：持久化 + 回到第 1 页（URL 同步由 effect 完成） */
  const changePageSize = useCallback(
    (n: number) => {
      persistPageSize(tableId, n)
      setPageSizeState(n)
      setCursorStack([null])
    },
    [tableId],
  )

  return { pageSize, changePageSize, cursorStack, pageIndex, cursor, goNext, goPrev, resetPage, restoring }
}
