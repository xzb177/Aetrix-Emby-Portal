// user_frontend/src/composables/useServerCardPrefs.ts
// 首页服务器卡片的展示偏好：模式（auto/cards/overview）+ 隐藏的服务器 id 列表。
// 偏好按用户 id 分别存储，读写失败一律静默（沿用 useTheme.ts 的做法）。
import { ref } from 'vue'
import { useUserStore } from '@/stores/user'

export type ServerCardMode = 'auto' | 'cards' | 'overview'

/** 存储键前缀，实际键 = 前缀 + `:u${userId}`（拿不到用户 id 时用前缀本身） */
const STORAGE_PREFIX = 'aetrix:home-server-cards:v1'

interface ServerCardPrefs {
  mode: ServerCardMode
  hidden: number[]
}

const DEFAULT_MODE: ServerCardMode = 'auto'

export function useServerCardPrefs() {
  const userStore = useUserStore()

  /** 当前用户对应的存储键 */
  function storageKey(): string {
    const id = userStore.user?.id
    return id == null ? STORAGE_PREFIX : `${STORAGE_PREFIX}:u${id}`
  }

  /** 读取偏好，任何异常都退化为默认值 */
  function readPrefs(): ServerCardPrefs {
    try {
      const raw = localStorage.getItem(storageKey())
      if (!raw) return { mode: DEFAULT_MODE, hidden: [] }

      const parsed = JSON.parse(raw) as Partial<ServerCardPrefs>
      const mode: ServerCardMode =
        parsed?.mode === 'cards' || parsed?.mode === 'overview' || parsed?.mode === 'auto'
          ? parsed.mode
          : DEFAULT_MODE
      const hidden = Array.isArray(parsed?.hidden)
        ? parsed.hidden.filter((n): n is number => typeof n === 'number' && Number.isFinite(n))
        : []

      return { mode, hidden }
    } catch {
      // 读取/解析失败：静默使用默认值
      return { mode: DEFAULT_MODE, hidden: [] }
    }
  }

  const initial = readPrefs()
  const mode = ref<ServerCardMode>(initial.mode)
  const hiddenIds = ref<number[]>(initial.hidden)

  /** 写回 localStorage，失败静默 */
  function writePrefs() {
    try {
      localStorage.setItem(
        storageKey(),
        JSON.stringify({ mode: mode.value, hidden: hiddenIds.value }),
      )
    } catch {
      // 写入失败：静默忽略
    }
  }

  /** 设置展示模式并立即持久化 */
  function setMode(m: ServerCardMode) {
    mode.value = m
    writePrefs()
  }

  /** 切换某个服务器的隐藏状态（在 hidden 里则移除，不在则加入）并立即持久化 */
  function toggleServer(id: number) {
    hiddenIds.value = hiddenIds.value.includes(id)
      ? hiddenIds.value.filter((x) => x !== id)
      : [...hiddenIds.value, id]
    writePrefs()
  }

  /** 某服是否在首页显示（不在 hidden 列表里即显示） */
  function isVisible(id: number): boolean {
    return !hiddenIds.value.includes(id)
  }

  /** 恢复默认偏好并清掉已存的记录 */
  function reset() {
    mode.value = DEFAULT_MODE
    hiddenIds.value = []
    try {
      localStorage.removeItem(storageKey())
    } catch {
      // 删除失败：静默忽略
    }
  }

  return { mode, hiddenIds, setMode, toggleServer, isVisible, reset }
}
