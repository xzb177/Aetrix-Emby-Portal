import { defineStore } from 'pinia'
import { ref } from 'vue'
import { panelApi, humanizeError } from '@/composables/usePanelApi'
import type { BoardData, FeedData } from '@/composables/usePanelApi'

/**
 * v4 面板数据 store（PR-2 地基）
 * - 内存缓存 TTL 60s（方案 §2.4）
 * - 接口失败时各区块显示纸色占位，不许白屏/转圈（由视图层处理 error 状态）
 */

const TTL_MS = 60_000

interface CacheEntry<T = unknown> {
  data: T | null
  error: string | null
  loading: boolean
  at: number
}

function freshEntry<T>(): CacheEntry<T> {
  return { data: null, error: null, loading: false, at: 0 }
}

export const usePanelStore = defineStore('panel', () => {
  const today = ref<CacheEntry>(freshEntry())
  const parcels = ref<CacheEntry>(freshEntry())
  const pulse = ref<CacheEntry>(freshEntry())
  const letter = ref<CacheEntry>(freshEntry())
  const board = ref<CacheEntry<BoardData>>(freshEntry())
  const collector = ref<CacheEntry>(freshEntry())
  const server = ref<CacheEntry>(freshEntry())
  /** 实时动态流（C档第3批）：独立于 /home 合并接口，单独加载 */
  const feed = ref<CacheEntry<FeedData>>(freshEntry())

  async function load<T>(entry: CacheEntry<T>, fetcher: () => Promise<T>, force = false) {
    const now = Date.now()
    if (!force && entry.data !== null && now - entry.at < TTL_MS) return
    if (entry.loading && !force) return
    entry.loading = true
    entry.error = null
    try {
      entry.data = await fetcher()
      entry.at = Date.now()
    } catch (e) {
      entry.error = humanizeError(e)
    } finally {
      entry.loading = false
    }
  }

  const loadToday = (force = false) => load(today.value, panelApi.today, force)
  const loadParcels = (force = false) => load(parcels.value, panelApi.parcels, force)
  const loadPulse = (force = false) => load(pulse.value, panelApi.pulse, force)
  const loadLetter = (force = false) => load(letter.value, panelApi.letter, force)

  /**
   * 首页合并加载（perf 2026-10-13）：一次请求拿回 today/parcels/pulse/letter，
   * 把 4 次跨洋 RTT 并成 1 次。后端没有 /home 接口（旧版本）时回退到逐个加载。
   */
  async function loadHome(force = false) {
    const now = Date.now()
    const entries = [today.value, parcels.value, pulse.value, letter.value, board.value]
    if (!force && entries.every((e) => e.data !== null && now - e.at < TTL_MS)) return
    if (!force && entries.some((e) => e.loading)) return
    for (const e of entries) { e.loading = true; e.error = null }
    try {
      const d = await panelApi.home()
      const at = Date.now()
      const parts: Array<[CacheEntry, unknown]> = [
        [today.value, d.today],
        [parcels.value, d.parcels],
        [pulse.value, d.pulse],
        [letter.value, d.letter],
        [board.value, d.board],
      ]
      for (const [e, data] of parts) {
        e.data = data
        e.error = null
        e.loading = false
        e.at = at
      }
    } catch {
      // 回退：逐个加载（兼容旧后端）
      for (const e of entries) { e.loading = false }
      await Promise.all([loadToday(force), loadParcels(force), loadPulse(force), loadLetter(force)])
    }
  }
  const loadCollector = (force = false) => load(collector.value, panelApi.collector, force)
  const loadServer = (force = false) => load(server.value, panelApi.server, force)
  const loadFeed = (force = false) => load(feed.value, panelApi.feed, force)

  /** 拆封：即时写后端（换设备同步），本地 optimistic 更新；失败抛错（携带人话原因），调用方 toast 提示 */
  async function unseal(itemId: string | number): Promise<void> {
    try {
      await panelApi.unseal(itemId)
    } catch (e) {
      throw new Error(humanizeError(e))
    }
    const data = parcels.value.data as { parcels?: Array<Record<string, unknown>> } | null
    const list = data?.parcels
    if (Array.isArray(list)) {
      const hit = list.find((p) => String(p.id) === String(itemId))
      if (hit) hit.unsealed = true
    }
    void loadToday(true) // 拆封成功后刷新 today 计数，不再等 60s TTL
  }

  /** 账号切换时清空全部面板缓存：防止新账号看到上一个账号的数据（TTL 内的串号问题） */
  function reset() {
    today.value = freshEntry()
    parcels.value = freshEntry()
    pulse.value = freshEntry()
    letter.value = freshEntry()
    board.value = freshEntry()
    collector.value = freshEntry()
    server.value = freshEntry()
    feed.value = freshEntry()
  }

  return {
    today, parcels, pulse, letter, board, collector, server, feed,
    loadToday, loadParcels, loadPulse, loadLetter, loadHome, loadCollector, loadServer, loadFeed,
    unseal, reset,
  }
})
