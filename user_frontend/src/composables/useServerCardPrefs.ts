// user_frontend/src/composables/useServerCardPrefs.ts
// 首页服务器卡片的展示偏好：模式（auto/cards/overview）+ 隐藏的服务器 id 列表。
// 偏好按用户 id 分别存储，读写失败一律静默（沿用 useTheme.ts 的做法）。
import { ref, watch } from 'vue'
import { useUserStore } from '@/stores/user'

export type ServerCardMode = 'auto' | 'cards' | 'overview'

/** 存储键前缀，实际键 = 前缀 + `:u${userId}`（拿不到用户 id 时用前缀本身） */
const STORAGE_PREFIX = 'aetrix:home-server-cards:v1'

interface ServerCardPrefs {
  mode: ServerCardMode
  hidden: number[]
}

const DEFAULT_MODE: ServerCardMode = 'auto'

/** 模块级单例状态：所有调用方共享同一份偏好，避免每个页面各持一份 */
const mode = ref<ServerCardMode>(DEFAULT_MODE)
const hiddenIds = ref<number[]>([])
/** 已经按哪个用户 id 载入过偏好（'@@unloaded' = 尚未载入；匿名用户 id 为 undefined/null 也会正常读取无后缀键） */
let prefsLoadedFor: number | null | undefined | '@@unloaded' = '@@unloaded'

/** 当前用户对应的存储键 */
function storageKey(userId: number | null | undefined): string {
  return userId == null ? STORAGE_PREFIX : `${STORAGE_PREFIX}:u${userId}`
}

/** 读取偏好，任何异常都退化为默认值 */
function readPrefs(userId: number | null | undefined): ServerCardPrefs {
  try {
    const raw = localStorage.getItem(storageKey(userId))
    if (!raw) return { mode: DEFAULT_MODE, hidden: [] }

    const parsed = JSON.parse(raw) as Partial<ServerCardPrefs>
    const parsedMode: ServerCardMode =
      parsed?.mode === 'cards' || parsed?.mode === 'overview' || parsed?.mode === 'auto'
        ? parsed.mode
        : DEFAULT_MODE
    const hidden = Array.isArray(parsed?.hidden)
      ? Array.from(
          new Set(
            parsed.hidden.filter((n): n is number => typeof n === 'number' && Number.isFinite(n)),
          ),
        )
      : []

    return { mode: parsedMode, hidden }
  } catch {
    // 读取/解析失败：静默使用默认值
    return { mode: DEFAULT_MODE, hidden: [] }
  }
}

/** 按用户 id 载入偏好（同一用户只读一次；用户切换时重新读） */
function ensurePrefsLoaded(userId: number | null | undefined) {
  if (prefsLoadedFor === userId) return
  prefsLoadedFor = userId
  const p = readPrefs(userId)
  mode.value = p.mode
  hiddenIds.value = p.hidden
}

/** 写回 localStorage 的防抖句柄 */
let writeTimer: ReturnType<typeof setTimeout> | null = null
/** 跨标签同步监听只注册一次 */
let storageHooked = false

if (!storageHooked) {
  storageHooked = true
  window.addEventListener('storage', (e) => {
    if (e.key && e.key.startsWith(STORAGE_PREFIX) && e.newValue) {
      try {
        const p = JSON.parse(e.newValue) as Partial<ServerCardPrefs>
        if (p.mode) mode.value = p.mode
        if (Array.isArray(p.hidden)) hiddenIds.value = p.hidden
      } catch {
        // 解析失败：静默忽略
      }
    }
  })
}

/** 写回 localStorage（200ms 防抖），失败静默 */
function writePrefs() {
  const key = storageKey(useUserStore().user?.id)
  if (writeTimer !== null) clearTimeout(writeTimer)
  writeTimer = setTimeout(() => {
    writeTimer = null
    try {
      localStorage.setItem(key, JSON.stringify({ mode: mode.value, hidden: hiddenIds.value }))
    } catch {
      // 写入失败：静默忽略
    }
  }, 200)
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
    localStorage.removeItem(storageKey(useUserStore().user?.id))
  } catch {
    // 删除失败：静默忽略
  }
}

export function useServerCardPrefs() {
  const userStore = useUserStore()

  // 用户 id 就绪 / 切换时（重新）载入对应偏好
  ensurePrefsLoaded(userStore.user?.id)
  watch(
    () => userStore.user?.id,
    (id) => ensurePrefsLoaded(id),
  )

  return { mode, hiddenIds, setMode, toggleServer, isVisible, reset }
}
