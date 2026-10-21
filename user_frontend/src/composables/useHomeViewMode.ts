// user_frontend/src/composables/useHomeViewMode.ts
// 用户端首页桌面形态偏好：'world'（探险世界地图式首页）| 'classic'（现有列表式首页）
// 存储键：aetrix:home-view:v1（登录用户追加 :u{userId} 后缀），存 JSON { mode }
// 默认 'world'。已登录且选了世界视图时，手机和桌面都走探险地图（手机版式在 WorldMap 里适配）。
import { ref, watch, computed } from 'vue'
import { useUserStore } from '@/stores/user'

export type HomeViewMode = 'world' | 'classic'
const STORAGE_PREFIX = 'aetrix:home-view:v1'
interface HomeViewPrefs { mode: HomeViewMode }
const DEFAULT_MODE: HomeViewMode = 'world'
const mode = ref<HomeViewMode>(DEFAULT_MODE)
let prefsLoadedFor: number | null | undefined | '@@unloaded' = '@@unloaded'
function storageKey(userId: number | null | undefined): string {
  return userId == null ? STORAGE_PREFIX : `${STORAGE_PREFIX}:u${userId}`
}
function readPrefs(userId: number | null | undefined): HomeViewPrefs {
  try {
    const raw = localStorage.getItem(storageKey(userId))
    if (!raw) return { mode: DEFAULT_MODE }
    const parsed = JSON.parse(raw) as Partial<HomeViewPrefs>
    // 只接受 'world' / 'classic'，其余退默认
    if (parsed.mode === 'world' || parsed.mode === 'classic') return { mode: parsed.mode }
    return { mode: DEFAULT_MODE }
  } catch { return { mode: DEFAULT_MODE } }
}
function ensureLoaded(userId: number | null | undefined) {
  if (prefsLoadedFor === userId) return
  prefsLoadedFor = userId
  const p = readPrefs(userId)
  mode.value = p.mode
}
let writeTimer: ReturnType<typeof setTimeout> | null = null
let storageHooked = false
if (!storageHooked && typeof window !== 'undefined') {
  storageHooked = true
  window.addEventListener('storage', (e) => {
    try {
      // 只响应当前登录用户自己的键：storage 事件只在其他标签页触发，
      // 其他用户标签页的写入不该翻转本页（按用户键精确比对，不是前缀匹配）
      if (e.key !== storageKey(useUserStore().user?.id)) return
      if (e.newValue == null) return
      const parsed = JSON.parse(e.newValue) as Partial<HomeViewPrefs>
      if (parsed.mode === 'world' || parsed.mode === 'classic') mode.value = parsed.mode
    } catch { /* 静默 */ }
  })
}
function writePrefs() {
  const key = storageKey(useUserStore().user?.id)
  if (writeTimer !== null) clearTimeout(writeTimer)
  writeTimer = setTimeout(() => {
    writeTimer = null
    try { localStorage.setItem(key, JSON.stringify({ mode: mode.value })) } catch { /* 静默 */ }
  }, 200)
}
function setMode(m: HomeViewMode) { mode.value = m; writePrefs() }

// 桌面断点：>=1024px；模块顶层可能没有 window（构建/测试环境），需守卫
const isDesktop = ref(false)
let mqlHooked = false
if (!mqlHooked && typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
  mqlHooked = true
  const mql = window.matchMedia('(min-width: 1024px)')
  isDesktop.value = mql.matches
  mql.addEventListener('change', (e) => { isDesktop.value = e.matches })
}

// user.id 变更监听只注册一次（app 生命周期常驻；composable 多次调用不再重复注册）
let idWatcherStarted = false
function startIdWatcherOnce() {
  if (idWatcherStarted) return
  idWatcherStarted = true
  watch(() => useUserStore().user?.id, (id) => ensureLoaded(id))
}

export function useHomeViewMode() {
  const userStore = useUserStore()
  ensureLoaded(userStore.user?.id)
  startIdWatcherOnce()
  const worldActive = computed(() => mode.value === 'world' && userStore.isLoggedIn)
  return { mode, setMode, isDesktop, worldActive }
}
