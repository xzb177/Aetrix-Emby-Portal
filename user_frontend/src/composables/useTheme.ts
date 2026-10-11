import { ref, computed, onMounted, onBeforeUnmount } from 'vue'

/**
 * 三档外观切换（v2.42.2）：黑暗 / 白日 / 跟随系统。
 *
 * 实际生效的主题（resolved）只有两个：dark（默认）与 light
 *（html[data-theme="light"]，独立调色的暖调纸白）。「跟随系统」是可选档——
 * resolved 由 prefers-color-scheme 实时决定，系统切换时无刷新跟随（监听 change 事件）。
 *
 * 缺省值（v2.53.x，用户决策 2026-10-11）：新用户首次进站默认 dark，不跟随系统；
 * localStorage('aetrix-theme') 有存值（light/dark/system）时以存的值为准，
 * 老用户已保存的偏好不受影响。
 *
 * 口径与 index.html 里的主题预套用脚本一致：localStorage('aetrix-theme')，
 * 提前写好 data-theme 避免首屏闪烁（FOUC）；这里负责后续切换与系统联动。
 *
 * 顶栏的 meta theme-color 跟随 resolved 切换：移动端浏览器工具栏不再固定深色。
 */

type ThemePreference = 'system' | 'light' | 'dark'
type ResolvedTheme = 'light' | 'dark'

const STORAGE_KEY = 'aetrix-theme'

const preference = ref<ThemePreference>('dark')
const systemLight = ref(false)
const metaTheme: HTMLMetaElement | null =
  typeof document !== 'undefined'
    ? document.querySelector('meta[name="theme-color"]')
    : null

function readStored(): ThemePreference {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    // 缺省 dark（用户决策 2026-10-11）；'system' 仍是合法显式选项，老用户存的值继续有效
    return stored === 'light' || stored === 'dark' || stored === 'system' ? stored : 'dark'
  } catch {
    return 'dark'
  }
}

function writeStored(value: ThemePreference) {
  try {
    localStorage.setItem(STORAGE_KEY, value)
  } catch {
    /* 隐私模式等存不了就算了：本次会话内仍生效 */
  }
}

/** 实际生效的主题：三档折叠成两档 */
const resolved = computed<ResolvedTheme>(() => {
  if (preference.value === 'system') return systemLight.value ? 'light' : 'dark'
  return preference.value
})

function applyTheme() {
  const html = document.documentElement
  if (resolved.value === 'light') html.setAttribute('data-theme', 'light')
  else html.removeAttribute('data-theme')
  // 浏览器工具栏跟底色（与 --au-bg 一致）：浅色影院大厅 #f5f1ea，深色暗房 #0c0a09
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
  // Safari < 14 只有 addListener，补上兜底
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

/** 首页 / 顶栏各调一次也安全：init 幂等，重复调用只是重复挂同一个监听前的清理 */
export function useTheme() {
  onMounted(() => {
    disposeTheme()
    initTheme()
  })
  onBeforeUnmount(disposeTheme)
  return { preference, resolved, setPreference }
}
