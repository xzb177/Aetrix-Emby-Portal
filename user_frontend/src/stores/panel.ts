import { defineStore } from 'pinia'
import { ref } from 'vue'
import { panelApi, humanizeError } from '@/composables/usePanelApi'

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
  const collector = ref<CacheEntry>(freshEntry())
  const server = ref<CacheEntry>(freshEntry())

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
  const loadCollector = (force = false) => load(collector.value, panelApi.collector, force)
  const loadServer = (force = false) => load(server.value, panelApi.server, force)

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

  /** 机关拨杆：先写后端，成功后 optimistic 更新本地（失败则回滚由调用方处理） */
  async function setServerToggle(key: string, on: boolean) {
    await panelApi.toggles({ key, on })
    const data = server.value.data as { toggles?: Array<Record<string, unknown>> } | null
    const list = data?.toggles
    if (Array.isArray(list)) {
      const hit = list.find((t) => String(t.key) === String(key))
      if (hit) hit.on = on
    }
  }

  return {
    today, parcels, pulse, letter, collector, server,
    loadToday, loadParcels, loadPulse, loadLetter, loadCollector, loadServer,
    unseal, setServerToggle,
  }
})
