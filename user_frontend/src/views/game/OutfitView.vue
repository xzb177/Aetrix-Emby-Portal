<template>
  <div class="v4-scope">
    <div class="v4-page">
      <!-- 日志头 -->
      <header class="v4-loghead">
        <span class="v4-stamp">衣帽间 · 试衣中</span>
        <h1>👕 换装</h1>
        <p>探险家的衣帽间 —— 点一套试穿，整个营地跟着换天色。</p>
      </header>

      <!-- 双 Tab：穿搭｜装备库（/game/outfit 单一入口，页内 state 切换不跳路由）；
           R27：左右箭头切换并移动焦点（WAI-ARIA tabs 键盘交互） -->
      <div class="tabs" role="tablist" aria-label="换装" @keydown="onTabKeydown">
        <button
          role="tab"
          :aria-selected="tab === 'outfit'"
          class="tab"
          :class="{ active: tab === 'outfit' }"
          @click="tab = 'outfit'"
        >
          穿搭
        </button>
        <button
          role="tab"
          :aria-selected="tab === 'gear'"
          class="tab"
          :class="{ active: tab === 'gear' }"
          @click="tab = 'gear'"
        >
          装备库
        </button>
      </div>

      <section
        v-if="tab === 'outfit'"
        role="tabpanel"
        aria-label="穿搭"
      >
      <div class="v4-sec">
        <div class="v4-sec-title">🎨 营地天色</div>
        <div class="v4-sec-sub">先试穿，满意了再点"就穿这套"</div>

        <div
          v-for="o in outfits"
          :key="o.id"
          class="outfit"
          :class="{ sel: picked === o.id }"
          role="button"
          tabindex="0"
          :aria-label="`试穿${o.label}${storedPref === o.id ? '（当前穿着）' : ''}`"
          @click="tryOn(o.id)"
          @keyup.enter="tryOn(o.id)"
          @keyup.space.prevent="tryOn(o.id)"
        >
          <span v-if="storedPref === o.id" class="now">当前</span>
          <div class="prev" aria-hidden="true">
            <span v-for="(c, i) in o.swatches" :key="i" class="pv" :style="{ background: c }"></span>
          </div>
          <div class="nm"><span class="em">{{ o.icon }}</span>{{ o.label }}</div>
          <div class="desc">{{ o.desc }}</div>
          <span class="tick">✓</span>
        </div>
      </div>

      <div class="note">
        💡 "跟随天色"会跟着手机的白天黑夜自动换，不用你操心。
      </div>
      </section>

      <section
        v-else
        role="tabpanel"
        aria-label="装备库"
      >
      <div class="v4-sec">
        <div class="v4-sec-title">🎒 装备库</div>
        <div class="v4-sec-sub">探险攒下的宝贝都在这儿</div>
        <!-- 纸片空态：首版先占位 -->
        <div class="gear-empty">
          <div class="gear-emoji" aria-hidden="true">📦</div>
          <div class="gear-title">还没有装备</div>
          <div class="gear-desc">去探险攒一件，回来就能在这儿看到它。</div>
          <router-link to="/game" class="gear-go" aria-label="去大本营逛逛">
            去大本营逛逛 →
          </router-link>
        </div>
      </div>
      </section>
    </div>

    <!-- 确认条：试穿中才出现 -->
    <div class="confirm" :class="{ show: picked !== storedPref }">
      <button @click="confirmWear">就穿这套</button>
    </div>

    <PanelTabbar />
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import PanelTabbar from '@/components/panel/PanelTabbar.vue'
import { useTheme } from '@/composables/useTheme'
import { useToast } from '@/composables/useToast'

/** 双 Tab：outfit 穿搭 / gear 装备库（内部 state，不跳路由） */
type OutfitTab = 'outfit' | 'gear'
const TAB_KEY = 'aetrix_outfit_tab_v1'

function readTab(): OutfitTab {
  try {
    const v = localStorage.getItem(TAB_KEY)
    return v === 'gear' ? 'gear' : 'outfit'
  } catch {
    return 'outfit'
  }
}
const tab = ref<OutfitTab>(readTab())
watch(tab, (v) => {
  try {
    localStorage.setItem(TAB_KEY, v)
  } catch {
    /* 记不住就每次默认穿搭，不打扰 */
  }
})

/** R27：Tab 键位导航 —— 左右箭头切换并把焦点跟过去（WAI-ARIA tabs 键盘交互） */
function onTabKeydown(e: KeyboardEvent) {
  if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return
  const tabs = Array.from(
    (e.currentTarget as HTMLElement).querySelectorAll<HTMLElement>('[role="tab"]'),
  )
  const i = tabs.indexOf(document.activeElement as HTMLElement)
  if (i < 0) return
  e.preventDefault()
  const next = (i + (e.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length
  tabs[next].click()
  tabs[next].focus()
}

type OutfitId = 'light' | 'dark' | 'system'

interface Outfit {
  id: OutfitId
  icon: string
  label: string
  desc: string
  swatches: string[]
}

/**
 * 三套营地天色 —— 严格对应 useTheme 的真实三档能力
 * （light=白日 / dark=黑暗 / system=跟随系统），不虚构不存在的主题。
 */
const outfits: Outfit[] = [
  {
    id: 'light',
    icon: '☀️',
    label: '白昼纸色',
    desc: '经典纸色，护眼耐看 —— 营地的老样子。',
    swatches: ['#F6F1E7', '#EFE7D6', '#2A2620', '#D9482B', '#C99B3F'],
  },
  {
    id: 'dark',
    icon: '🌙',
    label: '暗夜营地',
    desc: '油灯下的营地，夜猫子探险家专属。',
    swatches: ['#0C0A09', '#161310', '#EFE6D2', '#E2603F', '#C99B3F'],
  },
  {
    id: 'system',
    icon: '🌤️',
    label: '跟随天色',
    desc: '跟着手机的白天黑夜自动换，省心。',
    swatches: ['#F6F1E7', '#0C0A09', '#2A2620', '#D9482B', '#7FA6D9'],
  },
]

const { preference, setPreference } = useTheme()
const toast = useToast()

/** 已存档的选择（localStorage 里的值） */
const storedPref = ref<OutfitId>(preference.value)
/** 正在试穿的 */
const picked = ref<OutfitId>(preference.value)

const metaTheme: HTMLMetaElement | null =
  typeof document !== 'undefined'
    ? document.querySelector('meta[name="theme-color"]')
    : null

/** 试穿：只改 DOM 预览，不写 localStorage */
function applyPreview(id: OutfitId) {
  const html = document.documentElement
  const light = id === 'light'
  if (id === 'system') {
    // 跟随系统：按当前系统偏好预览
    const sysLight = window.matchMedia('(prefers-color-scheme: light)').matches
    if (sysLight) html.setAttribute('data-theme', 'light')
    else html.removeAttribute('data-theme')
    metaTheme?.setAttribute('content', sysLight ? '#f5f1ea' : '#0c0a09')
  } else {
    if (light) html.setAttribute('data-theme', 'light')
    else html.removeAttribute('data-theme')
    metaTheme?.setAttribute('content', light ? '#f5f1ea' : '#0c0a09')
  }
}

function tryOn(id: OutfitId) {
  picked.value = id
  applyPreview(id)
}

/** 确认：写进 useTheme，持久化 + 生效 */
function confirmWear() {
  setPreference(picked.value)
  storedPref.value = picked.value
  const o = outfits.find((x) => x.id === picked.value)
  toast.success(`换好了，${o?.icon}${o?.label}`)
}

/** 没点确认就离开：恢复存档的天色，不留"穿一半"的状态 */
onMounted(() => {
  // useTheme 在 onMounted 里才从 localStorage 读真实偏好，这里同步一次，
  // 否则 storedPref 会停在模块默认值上，"当前"徽标标错衣服
  storedPref.value = preference.value
  picked.value = preference.value
})
onBeforeUnmount(() => {
  if (picked.value !== storedPref.value) {
    const id = storedPref.value
    const html = document.documentElement
    const light = id === 'light' || (id !== 'dark' && window.matchMedia('(prefers-color-scheme: light)').matches)
    if (light) html.setAttribute('data-theme', 'light')
    else html.removeAttribute('data-theme')
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', light ? '#f5f1ea' : '#0c0a09')
    // useTheme 的监听还在，它会在自己的 onMounted 里重新 apply，这里只做兜底
  }
})
</script>

<style scoped>
/* 牛皮纸标签双 Tab */
.tabs {
  display: flex;
  gap: 10px;
  margin: 14px 0 4px;
}
.tab {
  flex: 1;
  font-size: 14px;
  font-weight: 800;
  letter-spacing: 4px;
  padding: 10px 0;
  background: var(--paper-card);
  color: var(--ink2);
  border: 1.5px dashed var(--line);
  border-radius: 4px 14px 4px 14px;
  cursor: pointer;
}
.tab.active {
  background: #e9d3a3;
  color: var(--ink);
  border: 2px solid var(--red);
  border-style: solid;
  box-shadow: 1px 2px 0 rgba(0, 0, 0, 0.08);
}
.tab:focus-visible {
  outline: 2px solid var(--red);
  outline-offset: 2px;
}

/* 装备库纸片空态 */
.gear-empty {
  background: var(--paper-card);
  border: 2px dashed var(--line);
  border-radius: 14px;
  padding: 36px 20px;
  text-align: center;
}
.gear-emoji {
  font-size: 44px;
  line-height: 1;
  margin-bottom: 12px;
}
.gear-title {
  font-size: 16px;
  font-weight: 800;
  letter-spacing: 2px;
}
.gear-desc {
  font-size: 13px;
  color: var(--ink2);
  margin: 8px 0 16px;
}
.gear-go {
  display: inline-block;
  font-size: 14px;
  font-weight: 800;
  color: #fff;
  background: var(--red);
  border-radius: 12px;
  padding: 10px 22px;
  text-decoration: none;
  box-shadow: 0 3px 0 #a33319;
}
.gear-go:active {
  transform: translateY(2px);
  box-shadow: none;
}
.gear-go:focus-visible {
  outline: 2px solid var(--red);
  outline-offset: 2px;
}

/* 服装卡：设计稿 game-theme.html 原值 */
.outfit {
  position: relative;
  background: var(--paper-card);
  border: 1.5px solid var(--line);
  border-radius: 12px;
  padding: 16px;
  margin-bottom: 12px;
  cursor: pointer;
}
.outfit.sel { border-color: var(--red); border-width: 2px; }
.outfit .now {
  position: absolute;
  top: -9px;
  right: 14px;
  font-size: 11px;
  font-weight: 800;
  color: #fff;
  background: var(--green);
  border-radius: 10px;
  padding: 2px 10px;
}
.outfit .prev { display: flex; gap: 6px; margin-bottom: 10px; }
.outfit .pv {
  width: 34px;
  height: 22px;
  border-radius: 6px;
  border: 1px solid var(--line);
}
.outfit .nm { font-size: 15px; font-weight: 800; }
.outfit .nm .em { margin-right: 6px; }
.outfit .desc { font-size: 12px; color: var(--ink2); margin-top: 4px; line-height: 1.6; }
.outfit .tick {
  position: absolute;
  right: 16px;
  top: 16px;
  width: 24px;
  height: 24px;
  border-radius: 50%;
  background: var(--red);
  color: #fff;
  font-size: 14px;
  font-weight: 800;
  display: none;
  align-items: center;
  justify-content: center;
}
.outfit.sel .tick { display: flex; }

.note {
  margin: 6px 20px 0;
  font-size: 12px;
  color: var(--ink2);
  line-height: 1.8;
}

/* 底部确认条：设计稿原值 */
.confirm {
  position: fixed;
  bottom: calc(76px + env(safe-area-inset-bottom));
  left: 0;
  right: 0;
  margin: 0 auto;
  width: calc(100% - 40px);
  max-width: 335px;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.25s;
  z-index: 50;
}
.confirm.show { opacity: 1; pointer-events: auto; }
.confirm button {
  width: 100%;
  background: var(--red);
  color: #fff;
  border: none;
  font-size: 16px;
  font-weight: 800;
  padding: 14px;
  border-radius: 12px;
  letter-spacing: 2px;
  box-shadow: 0 3px 0 #a33319;
  cursor: pointer;
}
.confirm button:active { transform: translateY(2px); box-shadow: none; }

@media (prefers-reduced-motion: reduce) {
  .confirm { transition: none; }
}

.outfit:focus-visible {
  outline: 2px solid var(--red);
  outline-offset: 2px;
}
</style>
