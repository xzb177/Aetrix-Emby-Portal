<template>
  <div class="v4-scope">
    <div class="v4-page">
      <!-- 日志头 -->
      <header class="v4-loghead">
        <span class="v4-stamp">衣帽间 · 试衣中</span>
        <h1>👕 换装</h1>
        <p>探险家的衣帽间 —— 点一套试穿，整个营地跟着换天色。</p>
      </header>

      <section class="v4-sec">
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
      </section>

      <div class="note">
        💡 "跟随天色"会跟着手机的白天黑夜自动换，不用你操心。
      </div>
    </div>

    <!-- 确认条：试穿中才出现 -->
    <div class="confirm" :class="{ show: picked !== storedPref }">
      <button @click="confirmWear">就穿这套</button>
    </div>

    <PanelTabbar />
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import PanelTabbar from '@/components/panel/PanelTabbar.vue'
import { useTheme } from '@/composables/useTheme'
import { useToast } from '@/composables/useToast'

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
/* 服装卡：设计稿 game-theme.html 原值 */
.outfit {
  position: relative;
  background: #FFFDF6;
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
