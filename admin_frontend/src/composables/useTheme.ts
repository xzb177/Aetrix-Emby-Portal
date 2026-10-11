import { ref, computed, onMounted, onBeforeUnmount } from 'vue'

/**
 * 管理后台外观切换（v2.42.4，与用户端 useTheme 同一套口径）：
 * 三档（跟随系统 / 白日 / 黑暗）持久化在 localStorage('aetrix-theme')，
 * 实际生效只有 dark / light 两档，切换只改 html[data-theme] ——
 * 浅色令牌定义在 aurora.css 的 html[data-theme='light'] 规则里（tokens.css 只是别名层），
 * Element Plus 变量在 element-plus-theme.css 的浅色块里同步分叉。
 */

type ThemePreference = 'system' | 'light' | 'dark'

const STORAGE_KEY = 'aetrix-theme'

// 顶栏 meta theme-color 跟随主题（移动端浏览器工具栏不再固定深色，与用户端同口径）
const metaTheme: HTMLMetaElement | null =
  typeof document !== 'undefined'
    ? document.querySelector('meta[name="theme-color"]')
    : null

const preference = ref<ThemePreference>('dark')
const systemLight = ref(false)

function readStored(): ThemePreference {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    return stored === 'light' || stored === 'dark' || stored === 'system' ? stored : 'dark'
  } catch {
    return 'dark'
  }
}

function writeStored(value: ThemePreference) {
  try {
    localStorage.setItem(STORAGE_KEY, value)
  } catch {
    /* 存不了就只本次会话生效 */
  }
}

/** 实际生效的主题：三档折叠成两档 */
const resolved = computed(() => {
  if (preference.value === 'system') return systemLight.value ? 'light' : 'dark'
  return preference.value
})

function applyTheme() {
  const html = document.documentElement
  if (resolved.value === 'light') html.setAttribute('data-theme', 'light')
  else html.removeAttribute('data-theme')
  // 浏览器工具栏跟画布色（aurora.css 的 --au-bg：深色暖黑 #0c0a09 / 白日奶油纸 #f5f1ea）
  metaTheme?.setAttribute('content', resolved.value === 'light' ? '#f5f1ea' : '#0c0a09')
}

function setPreference(value: ThemePreference) {
  preference.value = value
  writeStored(value)
  applyTheme()
}

let mql: MediaQueryList | null = null
let onSystemChange: ((e: MediaQueryListEvent) => void) | null = null

function initTheme() {
  preference.value = readStored()
  if (typeof window === 'undefined' || !window.matchMedia) {
    applyTheme()
    return
  }
  mql = window.matchMedia('(prefers-color-scheme: light)')
  systemLight.value = mql.matches
  onSystemChange = (e) => {
    systemLight.value = e.matches
    applyTheme()
  }
  if (mql.addEventListener) mql.addEventListener('change', onSystemChange)
  else if (mql.addListener) mql.addListener(onSystemChange)
  applyTheme()
}

function disposeTheme() {
  if (mql && onSystemChange) {
    if (mql.removeEventListener) mql.removeEventListener('change', onSystemChange)
    else if (mql.removeListener) mql.removeListener(onSystemChange)
  }
  mql = null
  onSystemChange = null
}

export function useAdminTheme() {
  onMounted(() => {
    disposeTheme()
    initTheme()
  })
  onBeforeUnmount(disposeTheme)
  return { preference, resolved, setPreference }
}
