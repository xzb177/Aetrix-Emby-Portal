<template>
  <div class="v4-scope">
    <div class="v4-page">
      <!-- 日志头 -->
      <header class="v4-loghead">
        <span class="v4-stamp">营地 · 探险地图</span>
        <h1><span aria-hidden="true">🗺️</span> 探险预告</h1>
        <p>新抵达的大陆，先睹为快。点"想去"加入探险计划。</p>
      </header>

      <!-- 时间线 -->
      <section class="v4-sec" :aria-busy="loading" aria-label="最近抵达">
        <h2 class="v4-sec-title"><span aria-hidden="true">🧭</span> 最近抵达</h2>
        <div class="v4-sec-sub">红点是今天，圆圈是近几天的日子</div>
        <div v-if="loading" class="sk-timeline" aria-hidden="true">
          <div class="v4-skeleton sk-tdate"></div>
          <div class="v4-skeleton sk-card"></div>
          <div class="v4-skeleton sk-card"></div>
          <div class="v4-skeleton sk-teaser"></div>
        </div>
        <div v-else-if="error" class="v4-err" role="alert">探险地图加载失败：{{ error }}<br><button class="v4-retry" @click="load">重试</button></div>
        <div v-else>
          <div v-if="days.length || teaserStandalone" class="timeline">
          <section
            v-for="day in days"
            :key="day.date"
            class="tday"
            :class="{ hot: isToday(day.date) }"
            :aria-label="dayLabel(day.date) + '的新东西'"
          >
            <h3 class="tdate">{{ dayLabel(day.date) }}</h3>
            <div class="cards">
              <div
                v-for="item in day.items.slice(0, expanded.has(day.date) ? day.items.length : 3)"
                :key="item.Id"
                class="treasure"
              >
                <div class="map" aria-hidden="true">{{ typeIcon(item.Type) }}</div>
                <div class="info">
                  <b>{{ item.Name }}</b>
                  <span><template v-if="item.LibraryName">{{ item.LibraryName }} · </template>{{ typeLabel(item.Type) }}</span>
                </div>
                <button
                  class="want"
                  :class="{ on: plan.has(item.Id) }"
                  :aria-pressed="plan.has(item.Id)"
                  :aria-label="item.Name + '，想去' + (plan.has(item.Id) ? '，已加入' : '')"
                  @click="togglePlan(item.Id)"
                >
                  <span class="lbl">{{ plan.has(item.Id) ? '已加入 ✓' : '想去' }}</span>
                </button>
              </div>
              <!-- R27：按实际拿到的条数判断，避免 day.count>3 但只有 3 条时显示「还有 0 件」 -->
              <button v-if="day.items.length > 3" class="more" :aria-expanded="expanded.has(day.date)" @click="toggleExpand(day.date)">{{ expanded.has(day.date) ? '收起 ↑' : '还有 ' + (day.items.length - 3) + ' 件，点我看全部 →' }}</button>
              <div v-if="day.items.length === 0" class="empty-day">这天营地很安静，没有新大陆</div>
            </div>
          </section>

          <!-- 未来：保密新大陆（后台可配的揭晓位，见 backend/emby_server/teaser.py）。
               未到揭晓日只显示保密文案；到日子自动换成揭晓后内容（可点跳转）。
               旧后端没有 teaser 字段时回退到原来的静态悬念卡，观感不变。 -->
          <section v-if="teaserVisible" class="tday future" :aria-label="effectiveTeaser?.status === 'revealed' ? '已揭晓的新大陆' : '未来保密中的新大陆'">
            <h3 class="tdate">
              <template v-if="effectiveTeaser?.status === 'revealed'">🎉 已揭晓</template>
              <template v-else>未来 · 保密中 <span v-if="teaserCountdownText" class="countdown">{{ teaserCountdownText }}</span></template>
            </h3>
            <div class="cards">
              <div
                class="treasure mystery"
                :class="{ clickable: !!teaserLink }"
                :role="teaserLink ? 'button' : undefined"
                :tabindex="teaserLink ? 0 : undefined"
                @click="onTeaserClick"
                @keydown.enter="onTeaserClick"
                @keydown.space.prevent="onTeaserClick"
              >
                <div class="map" aria-hidden="true">{{ effectiveTeaser?.status === 'revealed' ? '✨' : '🌫️' }}</div>
                <div class="info">
                  <b>{{ teaserTitle }}</b>
                  <span v-if="teaserSubtitle">{{ teaserSubtitle }}</span>
                </div>
              </div>
            </div>
          </section>
          </div>
          <!-- R27：无报错但一天都没有 → 空态说明（计划不持久化是 v1 设计，不改） -->
          <div v-if="!days.length && !teaserStandalone" class="v4-empty">这几天营地里还没有新大陆，过几天再来看看 🧭</div>
        </div>
      </section>

      <!-- 探险计划条 -->
      <div v-if="plan.size > 0" class="plan-bar">
        <span><span aria-hidden="true">🎒</span> 探险计划 <b>{{ plan.size }}</b> 个目的地</span>
        <!-- R41：误点"想去"只能再点一次取消 —— 加清空按钮一键清零（v1 计划不落库，清掉不影响后端） -->
        <button type="button" class="plan-clear" @click="clearPlan">清空</button>
      </div>
      <!-- R48B-14：清空后的读屏播报（常驻，不随 plan-bar 移除） -->
      <div class="sr-only" role="status">{{ planLiveMsg }}</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { fetchCalendar, StaleResponseError, type CalendarDay, type CalendarTeaser } from '@/api/calendar'
import { humanizeError } from '@/composables/usePanelApi'

const router = useRouter()
const days = ref<CalendarDay[]>([])
const loading = ref(true)
const error = ref('')
/* R48B-10：请求序号——只有最新请求能改 loading/状态 */
let loadSeq = 0

/** 保密新大陆占位卡：undefined = 旧后端（无 teaser 字段），回退静态悬念卡 */
const teaser = ref<CalendarTeaser | null | undefined>(undefined)
const FALLBACK_TEASER: CalendarTeaser = {
  status: 'secret',
  title: '? ? ?',
  subtitle: '神秘新大陆 · 到日子自动揭晓',
  countdown_days: null,
}
const effectiveTeaser = computed<CalendarTeaser | null>(
  () => (teaser.value === undefined ? FALLBACK_TEASER : teaser.value),
)
/** 后台关掉这张卡（teaser=null）时整块不渲染 */
const teaserVisible = computed(() => effectiveTeaser.value !== null)
/**
 * P1：后台显式配置了揭晓位（secret/revealed，非 undefined 回退、非 null 关闭）时，
 * 即使近 7 天无新抵达（days 为空），预告卡也要独立渲染。
 * undefined = 旧后端回退 → 沿旧行为，只随 days 出现；null = 后台关闭 → 不渲染。
 */
const teaserStandalone = computed(() => teaser.value !== undefined && teaser.value !== null)
/** 倒计时文案：没配日期沿用原来的 ???；揭晓日已过但内容没配（后台误配）时不显示倒计时 */
const teaserCountdownText = computed(() => {
  const t = effectiveTeaser.value
  if (!t || t.status !== 'secret') return ''
  const d = t.countdown_days
  if (d == null) return '???'
  if (d < 0) return ''
  if (d === 0) return '今天揭晓'
  return `还有 ${d} 天揭晓`
})
const teaserTitle = computed(() => effectiveTeaser.value?.title ?? '')
const teaserSubtitle = computed(() => {
  const t = effectiveTeaser.value
  if (!t) return ''
  return t.status === 'revealed' ? t.description : t.subtitle
})
const teaserLink = computed(() =>
  effectiveTeaser.value?.status === 'revealed' ? effectiveTeaser.value.link : null,
)
function onTeaserClick() {
  const link = teaserLink.value
  if (!link) return
  // / 开头走站内路由，其余（http(s)）新标签页打开
  if (link.startsWith('/')) void router.push(link)
  else window.open(link, '_blank', 'noopener')
}
/** 探险计划：本地 Set（v1 不落库） */
const plan = ref<Set<string>>(new Set())

function fmt(d: Date): string {
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${d.getFullYear()}-${m}-${day}`
}

function dayLabel(date: string): string {
  const [, m, d] = date.split('-').map(Number)
  if (isToday(date)) return `今天 · ${m}月${d}日`
  if (isYesterday(date)) return `昨天 · ${m}月${d}日`
  return `${m}月${d}日`
}

/* R48B-12：用日期本身判定今天/昨天，不用数组下标（后端顺序变化时下标不可靠） */
function isToday(date: string): boolean {
  return date === fmt(new Date())
}
function isYesterday(date: string): boolean {
  const y = new Date()
  y.setDate(y.getDate() - 1)
  return date === fmt(y)
}

function typeIcon(t?: string | null): string {
  if (t === 'Movie') return '🎬'
  if (t === 'Series') return '📺'
  if (t === 'Episode') return '🎞️'
  return '🗺️'
}

function typeLabel(t?: string | null): string {
  if (t === 'Movie') return '电影'
  if (t === 'Series') return '剧集'
  if (t === 'Episode') return '单集'
  return '影片'
}

function togglePlan(id: string) {
  const next = new Set(plan.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  plan.value = next
}

/** R48B-14：清空计划后的读屏播报（role=status，不抢焦点） */
const planLiveMsg = ref('')
let planLiveTimer: number | undefined

/** R41：探险计划一键清空（v1 不落库，只清本地 Set） */
function clearPlan() {
  plan.value = new Set()
  planLiveMsg.value = '已清空计划'
  window.clearTimeout(planLiveTimer)
  planLiveTimer = window.setTimeout(() => { planLiveMsg.value = '' }, 1500)
}

/** 展开某天全部藏宝（"还有 N 件"按钮用） */
const expanded = ref<Set<string>>(new Set())
function toggleExpand(date: string) {
  const next = new Set(expanded.value)
  if (next.has(date)) next.delete(date)
  else next.add(date)
  expanded.value = next
}

async function load() {
  const mySeq = ++loadSeq
  // 已有数据时的刷新（跨天定时器/回前台）静默替换，不闪骨架；首次加载才 show 骨架
  if (days.value.length === 0) loading.value = true
  error.value = ''
  try {
    const end = new Date()
    const start = new Date()
    start.setDate(start.getDate() - 6)
    const res = await fetchCalendar({ start: fmt(start), end: fmt(end) })
    // 最新在前
    days.value = [...res.days].reverse()
    // undefined = 旧后端无此字段 → 回退静态悬念卡；null = 后台关了这张卡
    teaser.value = res.teaser === undefined ? undefined : (res.teaser ?? null)
  } catch (e) {
    // 新请求已在路上：旧响应直接丢，不报错、不闪错误态
    if (e instanceof StaleResponseError) return
    // 旧请求的失败不许覆盖新请求：只有最新请求能写 error（成功路径已在 fetchCalendar 内守卫）
    if (mySeq !== loadSeq) return
    error.value = humanizeError(e)
  } finally {
    // R48B-10：旧请求不许碰 loading——只有最新请求能关闭它
    // （catch 里的 return 仍会执行 finally，所以必须用序号守卫）
    if (mySeq === loadSeq) loading.value = false
  }
}

/** 跨过揭晓日自动翻牌：对齐到次日 0 点重拉，自续期定时器 */
let midnightTimer = 0
/* R48B-11：上次成功发起 load() 时的本地日期（visibilitychange 跨天判定用） */
let lastLoadDay = ''
function scheduleMidnightReload() {
  const now = new Date()
  const next = new Date(now)
  next.setHours(24, 0, 0, 0)
  const delay = Math.max(1000, next.getTime() - now.getTime())
  window.clearTimeout(midnightTimer)
  midnightTimer = window.setTimeout(() => {
    lastLoadDay = fmt(new Date())
    void load()
    scheduleMidnightReload()
  }, delay)
}

/* R48B-11：从后台切回可见时，若已跨天则重拉（"今天/昨天"标签不错位） */
function onVisibilityChange() {
  if (document.visibilityState !== 'visible') return
  const today = fmt(new Date())
  if (today === lastLoadDay) return
  lastLoadDay = today
  void load()
}

onMounted(() => {
  lastLoadDay = fmt(new Date())
  load()
  scheduleMidnightReload()
  document.addEventListener('visibilitychange', onVisibilityChange)
})

onUnmounted(() => {
  window.clearTimeout(midnightTimer)
  window.clearTimeout(planLiveTimer)
  document.removeEventListener('visibilitychange', onVisibilityChange)
})
</script>

<style scoped>
/* 时间线：设计稿 game-calendar.html 原值 */
.timeline { position: relative; padding-left: 26px; }
.timeline::before {
  content: "";
  position: absolute;
  left: 8px;
  top: 6px;
  bottom: 6px;
  width: 2px;
  background: var(--line);
}
.tday { position: relative; margin-bottom: 16px; }
.tday::before {
  content: "";
  position: absolute;
  left: -24px;
  top: 4px;
  width: 12px;
  height: 12px;
  border-radius: var(--paper-r-full, 50%);
  background: var(--paper);
  border: 2.5px solid var(--ink2);
}
.tday.hot::before {
  border-color: var(--red);
  background: var(--red);
  box-shadow: 0 0 0 4px rgba(217, 72, 43, 0.15);
}
.tday.future::before { border-color: var(--gold); border-style: dashed; }
.tdate {
  font-size: 12px;
  font-weight: 800;
  color: var(--ink2);
  margin-bottom: 8px;
  letter-spacing: 1px;
}
.tday.hot .tdate { color: var(--red); }
.cards { display: flex; flex-direction: column; gap: 10px; }
.treasure {
  background: var(--paper-card);
  border: 1.5px solid var(--line);
  border-radius: var(--paper-r-md, 10px);
  padding: 12px 14px;
  display: flex;
  gap: 12px;
  align-items: center;
  box-shadow: 0 2px 6px rgba(42, 38, 32, 0.05);
}
.treasure .map { font-size: 30px; flex-shrink: 0; }
.treasure .info { flex: 1; min-width: 0; }
.treasure .info b {
  font-size: 14px;
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.treasure .info span { font-size: 11px; color: var(--ink2); }
.treasure .want {
  flex-shrink: 0;
  position: relative;
  /* R41：纵向 padding 13px→15px —— 原注释声称 44px，实算约 40px，提到 15px 才诚实达标 */
  background: transparent;
  border: none;
  font-size: 12px;
  font-weight: 700;
  font-family: inherit;
  padding: 15px 12px;
  margin: -6px 0;
  cursor: pointer;
  color: var(--ink);
}
/* 视觉药丸画在 ::before 上，随 padding 偏移（上下各 8px，药丸高度与原来像素一致） */
.treasure .want::before {
  content: "";
  position: absolute;
  top: 8px; right: 0; bottom: 8px; left: 0;
  background: var(--paper2);
  border: 1.5px solid var(--ink);
  border-radius: var(--paper-r-xl, 16px);
}
.treasure .want .lbl { position: relative; }
.treasure .want.on { color: var(--on-dark, #fff); }
.treasure .want.on::before { background: var(--ink); }
.treasure.mystery { background: var(--paper2); border-style: dashed; }
.treasure.mystery .map { filter: grayscale(1) blur(1px); }
.treasure.mystery .info b { letter-spacing: 4px; color: var(--ink2); }
/* 揭晓后可点的卡：手型 + 去掉虚线灰度，仍是同一张卡的语言 */
.treasure.mystery.clickable { cursor: pointer; }
.treasure.mystery.clickable .map { filter: none; }
.countdown {
  display: inline-block;
  background: var(--gold);
  color: var(--ink);
  font-size: 10px;
  font-weight: 800;
  padding: 2px 8px;
  border-radius: var(--paper-r-md, 10px);
  margin-left: 6px;
  vertical-align: middle;
}
.more {
  font-size: 11px; color: var(--ink2);
  background: none; border: none; cursor: pointer; text-align: left;
  /* R41：纵向 padding 14px→16px —— 15px 实算 43.2px 仍差一截，16px 才诚实达标 44px（文案行视觉不变） */
  padding: 16px 0; margin: -12px 0;
  font-family: inherit;
}
.empty-day { font-size: 11px; color: var(--ink2); padding: 2px 0; }
.plan-bar {
  position: sticky;
  /* R48B-13：跟随 tabbar 高度 + iOS 安全区，不再写死 90px */
  bottom: calc(var(--tabbar-h, 56px) + env(safe-area-inset-bottom, 0px) + 12px);
  margin: 16px 20px 0;
  background: var(--ink);
  color: var(--paper, #F6F1E7);
  border-radius: var(--paper-r-lg, 12px);
  padding: 12px 16px;
  font-size: 13px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  box-shadow: 0 4px 14px rgba(42, 38, 32, 0.25);
  /* F10：显式层级——低于 tabbar 的 50、高于内容区 relative 元素，不依赖 DOM 顺序 */
  z-index: 10;
}
.plan-bar b { color: var(--ember, #FFB25E); }
/* R41：清空按钮 —— 文字按钮，触点纵向 44px、横向触点 ≥44px（透明 padding + 负 margin，条的高度不变） */
.plan-bar .plan-clear {
  background: none;
  border: none;
  color: var(--ember, #FFB25E);
  font-size: 13px;
  font-weight: 700;
  font-family: inherit;
  padding: 15px 10px;
  margin: -15px -10px -15px 0;
  cursor: pointer;
}
.plan-bar .plan-clear:focus-visible { outline: 2px solid var(--ember, #FFB25E); outline-offset: -4px; }

/* R48B-14：读屏播报节点（视觉隐藏） */
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}
.tdate { margin: 0 0 8px; } /* F1：h3 默认 margin 归零但保留设计稿的 8px 下间距，观感不变 */
/* F6：探险预告骨架——贴近真实结构，避免 220px 单块造成的布局跳动 */
.sk-timeline { display: flex; flex-direction: column; gap: 10px; padding-left: 26px; }
.sk-tdate { height: 18px; width: 40%; }
.sk-card { height: 60px; }
.sk-teaser { height: 64px; }
</style>
