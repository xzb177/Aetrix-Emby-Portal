<template>
  <div class="v4-scope">
    <div class="v4-page">
      <!-- 日志头 -->
      <header class="v4-loghead">
        <span class="v4-stamp">衣帽间 · 试衣中</span>
        <h1><span aria-hidden="true">👕</span> 换装</h1>
        <p>探险家的衣帽间 —— 点一套看看配色，满意了再点"就穿这套"。</p>
        <!-- R41：原"整个营地跟着换天色"是虚假承诺 —— v4 纸色浅色是既定硬约束（路由强制 light），试穿暗色在 v4 页不可见 -->
      </header>

      <!-- 双 Tab：穿搭｜装备库（/game/outfit 单一入口，页内 state 切换不跳路由）；
           R27：左右箭头切换并移动焦点（WAI-ARIA tabs 键盘交互） -->
      <div class="tabs" role="tablist" aria-label="换装" @keydown="onTabKeydown">
        <button
          role="tab"
          id="tab-outfit"
          aria-controls="panel-outfit"
          :aria-selected="tab === 'outfit'"
          :tabindex="tab === 'outfit' ? 0 : -1"
          class="tab"
          :class="{ active: tab === 'outfit' }"
          @click="tab = 'outfit'"
        >
          穿搭
        </button>
        <button
          role="tab"
          id="tab-gear"
          aria-controls="panel-gear"
          :aria-selected="tab === 'gear'"
          :tabindex="tab === 'gear' ? 0 : -1"
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
        id="panel-outfit"
        aria-labelledby="tab-outfit"
        aria-label="穿搭"
      >
      <div class="v4-sec">
        <div class="v4-sec-title"><span aria-hidden="true">🎨</span> 营地天色</div>
        <div class="v4-sec-sub">先试穿，满意了再点"就穿这套"</div>

        <template v-if="outfits.length > 0">
        <div
          v-for="o in outfits"
          :key="o.id"
          class="outfit"
          :class="{ sel: picked === o.id }"
          role="button"
          tabindex="0"
          :aria-label="`试穿${o.label}${storedPref === o.id ? '（当前穿着）' : ''}${picked === o.id ? '（试穿中）' : ''}`"
          @click="tryOn(o.id)"
          @keyup.enter="tryOn(o.id)"
          @keydown.space.prevent="tryOn(o.id)"
        >
          <span v-if="storedPref === o.id" class="now">当前</span>
          <div class="prev" aria-hidden="true">
            <span v-for="(c, i) in o.swatches" :key="i" class="pv" :style="{ background: c }"></span>
          </div>
          <div class="nm"><span class="em">{{ o.icon }}</span>{{ o.label }}</div>
          <div class="desc">{{ o.desc }}</div>
          <span class="tick">✓</span>
        </div>
        </template>
        <div v-else class="v4-empty">暂时没有可换的配色，稍后再来看看 <span aria-hidden="true">✨</span></div>
      </div>

      <div class="note">
        <span aria-hidden="true">💡</span> "跟随天色"会跟着手机的白天黑夜自动换，不用你操心。
      </div>
      </section>

      <section
        v-else
        role="tabpanel"
        id="panel-gear"
        aria-labelledby="tab-gear"
        aria-label="装备库"
      >
      <div class="v4-sec">
        <div class="v4-sec-title">🎒 装备库 · 徽章陈列柜</div>
        <div class="v4-sec-sub">探险攒下的徽章都在这儿 —— 点一枚戴上，再点右上角头像就能看到它</div>
        <!-- 加载中 -->
        <div v-if="badgeLoading" class="badge-wall" role="status" aria-live="polite">
          <span style="position:absolute;width:1px;height:1px;margin:-1px;padding:0;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0">正在取回徽章…</span>
          <div v-for="i in 4" :key="i" class="v4-skeleton badge-skel" aria-hidden="true"></div>
        </div>
        <!-- 加载失败 -->
        <div v-else-if="badgeError" class="v4-err">
          徽章柜没打开：{{ badgeError }}<br>
          <button class="v4-retry" @click="loadBadges">重试</button>
        </div>
        <!-- 徽章墙 -->
        <div v-else-if="badges.length > 0" class="badge-wall">
          <!-- R41/R54：外层是真正的 button（role+tabindex+键盘），未解锁卡的 aria-label 不带真实名/des（防泄底）；
               内层原生 <button>（戴上/取下）用 @click.stop 承接，键盘两条路径不打架。 -->
          <div
            v-for="b in badges"
            :key="b.key"
            class="badge"
            :class="{ locked: !b.unlocked, worn: wornBadgeKey === b.key }"
            role="button"
            tabindex="0"
            :aria-label="b.unlocked ? `${b.name}：${b.desc}，${wornBadgeKey === b.key ? '佩戴中' : '已解锁'}` : '未解锁的徽章，还没发生的事'"
            @click="onBadgeClick(b)"
            @keydown.enter.self.prevent="onBadgeClick(b)"
            @keydown.space.self.prevent="onBadgeClick(b)"
          >
            <span v-if="wornBadgeKey === b.key" class="worn-tag">佩戴中</span>
            <div class="badge-icon" aria-hidden="true">{{ b.unlocked ? b.icon : '🌫️' }}</div>
            <div class="badge-name">{{ b.unlocked ? b.name : '???' }}</div>
            <div class="badge-desc">{{ b.unlocked ? b.desc : '还没发生的事' }}</div>
            <button
              v-if="b.unlocked && wornBadgeKey !== b.key"
              class="wear-btn"
              :disabled="badgeOpPending"
              @click.stop="wearBadge(b)"
              @keyup.stop
              :aria-label="`佩戴${b.name}徽章`"
              :aria-busy="badgeOpPending"
            >
              戴上
            </button>
            <button
              v-if="wornBadgeKey === b.key"
              class="wear-btn off"
              :disabled="badgeOpPending"
              @click.stop="unwearBadge()"
              @keyup.stop
              aria-label="取下徽章"
              :aria-busy="badgeOpPending"
            >
              取下
            </button>
          </div>
        </div>
        <!-- 空 -->
        <div v-else class="v4-empty">还没有徽章，去探险攒一枚 ✨</div>
        <!-- 新解锁提示 -->
        <div v-if="newUnlockNames.length > 0" class="v4-fresh" style="margin-top:12px" role="status" aria-live="polite">
          <span aria-hidden="true">🎉</span> 新徽章解锁：{{ newUnlockNames.join('、') }}
        </div>
      </div>
      <div class="note">
        💡 换装是换衣服（营地天色），徽章是别在衣服上的荣誉 —— 两不耽误。
      </div>
      </section>
    </div>

    <!-- 确认条：试穿中才出现 -->
    <div class="confirm" :class="{ show: picked !== storedPref }">
      <button @click="confirmWear">就穿这套</button>
    </div>

  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch, computed } from 'vue'
import { useTheme } from '@/composables/useTheme'
import { useToast } from '@/composables/useToast'
import { panelApi, humanizeError, type Achievement } from '@/composables/usePanelApi'
import { useUserStore } from '@/stores/user'
import type { WornBadge } from '@/api'

/** 旧版佩戴只存浏览器本地（键 aetrix_worn_badge_v1），会串号——已改存账号。
 *  这个键仅供一次性迁移与清理：把老用户本地戴着的徽章搬进账号后即删除。 */
const LEGACY_WORN_BADGE_KEY = 'aetrix_worn_badge_v1'

function readLegacyWornBadgeKey(): string | null {
  let raw: string | null = null
  try {
    raw = localStorage.getItem(LEGACY_WORN_BADGE_KEY)
  } catch {
    return null
  }
  if (!raw) return null
  try {
    const o = JSON.parse(raw)
    // JSON 字符串 "pioneer" 会解析成 string 分支；对象写法走 o.key
    return typeof o === 'string' ? o : (o && o.key ? o.key : null)
  } catch {
    // JSON.parse 抛错 = 旧版直接存的裸 key 字符串（如 pioneer），裸 key 本身就是 key
    return raw
  }
}

function clearLegacyWornBadgeKey(): void {
  try {
    localStorage.removeItem(LEGACY_WORN_BADGE_KEY)
  } catch { /* 忽略 */ }
}

/* ---------- 徽章墙（装备库） ---------- */
const badges = ref<Achievement[]>([])
const badgeLoading = ref(true)
const badgeError = ref('')
const newUnlockKeys = ref<string[]>([])
const wornBadgeKey = ref<string | null>(null)
const badgeOpPending = ref(false)

const newUnlockNames = computed(() => {
  if (newUnlockKeys.value.length === 0) return []
  const byKey = new Map(badges.value.map((b) => [b.key, b.name]))
  return newUnlockKeys.value.map((k) => byKey.get(k) || '新成就')
})

// 并发防护：只让最新一次请求写 badges / newUnlockKeys / badgeError 与 loading（R54-52）
let badgesReqSeq = 0

// R63A2-1：佩戴写入代次。wearBadge / unwearBadge / migrateLegacyWornBadge
// 成功写入佩戴态后自增，用于让在途的 loadBadges 旧快照跳过佩戴态同步
let wornWriteSeq = 0

async function loadBadges() {
  const seq = ++badgesReqSeq
  // R63A2-1：记下发请求时的佩戴写入代次
  const wornSeqAtIssue = wornWriteSeq
  badgeLoading.value = true
  badgeError.value = ''
  try {
    const data = await panelApi.collector()
    if (seq !== badgesReqSeq) return
    badges.value = Array.isArray(data.achievements) ? data.achievements : []
    newUnlockKeys.value = Array.isArray(data.new_unlocks) ? data.new_unlocks.map(String) : []
    // R63A2-1：期间发生过佩戴写入，不拿旧快照覆盖用户的佩戴选择（列表已更新，直接收尾）
    if (wornWriteSeq !== wornSeqAtIssue) return
    // 佩戴状态以账号为准（后端已校验解锁/上架：下架或未解锁的不会下发）
    wornBadgeKey.value = data.worn_badge?.key ?? null
    // R58-F2：账号里佩戴态变化（如管理员下架徽章）时同步 store，
    // 头像菜单（读 store）才不会继续显示已下架的徽章
    useUserStore().updateUser({ worn_badge: data.worn_badge ?? null })
    await migrateLegacyWornBadge(data.worn_badge)
  } catch (e) {
    if (seq !== badgesReqSeq) return
    badgeError.value = humanizeError(e)
  } finally {
    if (seq === badgesReqSeq) badgeLoading.value = false
  }
}

// R63A2-3：佩戴操作 10s 超时兜底。setWornBadge 不接受 AbortSignal，
// 故在调用处用 Promise.race 包 10s 超时；超时按失败走，finally 照常复位 pending。
function withBadgeOpTimeout<T>(p: Promise<T>, ms = 10000): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined
  return Promise.race([
    p,
    new Promise<never>((_, reject) => {
      timer = setTimeout(() => reject(new Error('佩戴操作超时，请重试')), ms)
    }),
  ]).finally(() => clearTimeout(timer))
}

/** 旧版本地佩戴键一次性迁移：账号里还没有佩戴记录、且本地这枚是本账号
 *  已解锁的，才搬进账号；搬完（或确认无需搬）清掉本地键。迁移失败不清键，
 *  下次进装备库再试。 */
async function migrateLegacyWornBadge(serverWorn: WornBadge | null | undefined) {
  const legacyKey = readLegacyWornBadgeKey()
  if (!legacyKey) return
  if (serverWorn) {
    clearLegacyWornBadgeKey()
    return
  }
  const target = badges.value.find((b) => b.key === legacyKey && b.unlocked)
  if (!target) {
    clearLegacyWornBadgeKey()
    return
  }
  if (badgeOpPending.value) return
  badgeOpPending.value = true
  try {
    const res = await withBadgeOpTimeout(panelApi.setWornBadge(legacyKey))
    wornBadgeKey.value = res.worn_badge?.key ?? legacyKey
    useUserStore().updateUser({
      worn_badge: res.worn_badge ?? { key: target.key, name: target.name, icon: target.icon },
    })
    wornWriteSeq++ // R63A2-1：佩戴写入成功，通知在途的 loadBadges 跳过旧快照
    clearLegacyWornBadgeKey()
  } catch (e) {
    // R63A2-2：迁移失败不再静默，保留"不清本地键、下次再试"的现有逻辑
    console.warn('[outfit] 迁移本地佩戴徽章失败', legacyKey, e)
  }
  finally { badgeOpPending.value = false }
}

async function wearBadge(b: Achievement) {
  if (badgeOpPending.value) return
  badgeOpPending.value = true
  try {
    const res = await withBadgeOpTimeout(panelApi.setWornBadge(b.key))
    wornBadgeKey.value = res.worn_badge?.key ?? b.key
    useUserStore().updateUser({
      worn_badge: res.worn_badge ?? { key: b.key, name: b.name, icon: b.icon },
    })
    wornWriteSeq++ // R63A2-1：佩戴写入成功，通知在途的 loadBadges 跳过旧快照
    toast.success(`戴上了 ${b.icon}${b.name} 徽章`)
  } catch (e) {
    toast.error(humanizeError(e))
  } finally {
    badgeOpPending.value = false
  }
}

async function unwearBadge() {
  if (badgeOpPending.value) return
  badgeOpPending.value = true
  try {
    await withBadgeOpTimeout(panelApi.setWornBadge(null))
    wornBadgeKey.value = null
    useUserStore().updateUser({ worn_badge: null })
    wornWriteSeq++ // R63A2-1：佩戴写入成功，通知在途的 loadBadges 跳过旧快照
    // R45：取下徽章也要给反馈（wearBadge 有 toast.success，这里对齐用 toast.info）
    toast.info('已取下徽章')
  } catch (e) {
    toast.error(humanizeError(e))
  } finally {
    badgeOpPending.value = false
  }
}

function onBadgeClick(b: Achievement) {
  if (!b.unlocked) {
    toast.info(`还没解锁：${b.desc}`)
    return
  }
  if (wornBadgeKey.value === b.key) unwearBadge()
  else wearBadge(b)
}

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
    swatches: ['var(--paper, #F6F1E7)', 'var(--paper2, #EFE7D6)', 'var(--ink, #2A2620)', 'var(--red, #D9482B)', 'var(--gold, #C99B3F)'],
  },
  {
    id: 'dark',
    icon: '🌙',
    label: '暗夜营地',
    desc: '油灯下的营地，夜猫子探险家专属。',
    swatches: ['#0C0A09', '#161310', '#EFE6D2', '#E2603F', 'var(--gold, #C99B3F)'],
  },
  {
    id: 'system',
    icon: '🌤️',
    label: '跟随天色',
    desc: '跟着手机的白天黑夜自动换，省心。',
    swatches: ['var(--paper, #F6F1E7)', '#0C0A09', 'var(--ink, #2A2620)', 'var(--red, #D9482B)', '#7FA6D9'],
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
  // 徽章墙数据：进页面就拉，Tab 切换时不用重复拉
  void loadBadges()
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
  /* R28：触点提到 44px 高（视觉不变） */
  min-height: 44px;
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

/* 徽章墙 */
.badge-wall {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
}
/* R54-55：窄屏 3 列会把正文挤到折行、同行按钮错位，降到 2 列 */
@media (max-width: 399px) {
  .badge-wall { grid-template-columns: repeat(2, 1fr); }
}
.badge {
  position: relative;
  background: var(--paper-card);
  border: 1.5px solid var(--line);
  border-radius: var(--paper-r-lg, 12px);
  padding: 14px 8px 12px;
  text-align: center;
  cursor: pointer;
}
.badge:focus-visible {
  outline: 2px solid var(--red);
  outline-offset: 2px;
}
.badge.locked {
  border-style: dashed;
  border-color: var(--line);
}
/* 骨架占位卡：复用 .badge-wall 栅格，占位高度贴近真实徽章卡，避免数据到达时跳动 */
.badge-skel {
  min-height: 132px;
  border-radius: var(--paper-r-md, 10px);
}
.badge.locked .badge-icon {
  opacity: 0.55;
}
.badge.worn {
  border-color: var(--red);
  border-width: 2px;
  box-shadow: 0 2px 0 rgba(217, 72, 43, 0.25);
}
.badge-icon {
  font-size: 32px;
  line-height: 1.2;
}
.badge-name {
  font-size: 12px;
  font-weight: 800;
  margin-top: 6px;
  letter-spacing: 1px;
}
.badge-desc {
  font-size: 11px;
  color: var(--ink2);
  margin-top: 4px;
  line-height: 1.5;
  min-height: 33px;
}
.worn-tag {
  position: absolute;
  top: -8px;
  right: 8px;
  font-size: 11px;
  font-weight: 800;
  color: var(--on-dark, #fff);
  background: var(--red);
  border-radius: var(--paper-r-md, 10px);
  padding: 2px 8px;
  letter-spacing: 1px;
}
.wear-btn {
  position: relative;
  /* 触点 44px+：药丸由按钮自身边框绘制，触点边界与可见药丸一致，不再用负 margin 侵入上方描述文字 */
  margin: 0;
  min-height: 44px;
  font-size: 11px;
  font-weight: 800;
  color: var(--red);
  background: transparent;
  border: 1.5px solid var(--red);
  border-radius: var(--paper-r-md, 10px);
  padding: 0 14px;
  cursor: pointer;
  letter-spacing: 1px;
}
.wear-btn.off {
  color: var(--ink2);
  border-color: var(--line);
}
.wear-btn:focus-visible {
  outline: 2px solid var(--red);
  outline-offset: 2px;
}
.wear-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* 服装卡：设计稿 game-theme.html 原值 */
.outfit {
  position: relative;
  background: var(--paper-card);
  border: 1.5px solid var(--line);
  border-radius: var(--paper-r-lg, 12px);
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
  color: var(--on-dark, #fff);
  background: var(--green);
  border-radius: var(--paper-r-md, 10px);
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
  border-radius: var(--paper-r-full, 50%);
  background: var(--red);
  color: var(--on-dark, #fff);
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
/* R37 P3-6：隐藏态加 visibility:hidden —— opacity:0 的按钮仍在 Tab 顺序里、读屏仍朗读 */
.confirm {
  position: fixed;
  bottom: calc(76px + env(safe-area-inset-bottom));
  left: 0;
  right: 0;
  margin: 0 auto;
  width: calc(100% - 40px);
  max-width: 335px;
  opacity: 0;
  visibility: hidden;
  pointer-events: none;
  transition: opacity 0.25s, visibility 0.25s;
  z-index: 50;
}
.confirm.show { opacity: 1; visibility: visible; pointer-events: auto; }
.confirm button {
  width: 100%;
  background: var(--red);
  color: var(--on-dark, #fff);
  border: none;
  font-size: 16px;
  font-weight: 800;
  padding: 14px;
  border-radius: var(--paper-r-lg, 12px);
  letter-spacing: 2px;
  box-shadow: 0 3px 0 var(--red-deep, #a33319);
  cursor: pointer;
}
.confirm button:focus-visible {
  outline: 2px solid var(--red-deep, #a33319);
  outline-offset: 2px;
}
.confirm button:active { transform: translateY(2px); box-shadow: none; }

@media (prefers-reduced-motion: reduce) {
  .confirm { transition: none; }
  /* 位移类 :active 按压动效在减弱动态下关闭 */
  .confirm button:active { transform: none; }
}

.outfit:focus-visible {
  outline: 2px solid var(--red);
  outline-offset: 2px;
}
</style>
