<template>
  <div class="v4-scope">
    <div class="v4-page">
      <!-- 日志头 -->
      <header class="v4-loghead" :aria-busy="statusLoading">
        <span class="v4-stamp">营地 · 报到处</span>
        <h1><span aria-hidden="true">📋</span> 每日报到</h1>
        <p>探险家每天到营地报到，盖章打卡，领能量。</p>
        <div v-if="status" class="v4-fire">
          <template v-if="status.streak > 0"><span aria-hidden="true">🔥</span> 已连报 <b>{{ status.streak }}</b> 天
            <!-- R43：后端成就名叫"连击"（panel_achievements.py streak7），不叫"金章"，文案跟后端对齐，不许发明 -->
            <span v-if="!status.checked_today"> · 今天还没报到</span>
            <span v-if="status.streak >= 7"> · <span aria-hidden="true">🔥</span> 连击达成</span>
          </template>
          <template v-else>今天开始，连续报到第 1 天</template>
          <span v-if="statusStale" class="v4-stale">（数据可能过期）</span>
        </div>
        <div v-else-if="statusLoading" class="v4-skeleton sk-fire" aria-hidden="true"></div>
        <div v-else-if="statusError" class="v4-err" role="alert">连报数据加载失败：{{ statusError }}<br><button type="button" class="v4-retry" @click="load">重试</button></div>
      </header>

      <!-- 探险体力 -->
      <StaminaBar ref="staminaBar" />

      <!-- 报到簿 -->
      <section class="v4-sec" :aria-busy="statusLoading">
        <div class="v4-sec-title"><span aria-hidden="true">📖</span> 报到簿 · {{ monthLabel }}</div>
        <!-- R48：子文案跟着状态走，加载/失败时不显示行动文案 -->
        <div v-if="!statusLoading && !statusError" class="v4-sec-sub">
          <template v-if="status?.checked_today">今日已盖章 <span aria-hidden="true">✅</span></template>
          <template v-else>点击下方"报到"按钮盖章</template>
        </div>
        <div v-if="statusLoading" class="v4-skeleton sk-book" aria-hidden="true"></div>
        <div v-else-if="statusError" class="v4-err" role="alert">报到簿加载失败：{{ statusError }}<br><button type="button" class="v4-retry" @click="load">重试</button></div>
        <div v-else class="book">
          <div class="book-head">
            <b>{{ year }}年{{ monthLabel }}</b>
            <span>本月已报 {{ signedCount }} 天</span>
          </div>
          <div class="cal" role="grid">
            <div
              v-for="d in daysInMonth"
              :key="d"
              class="day"
              role="gridcell"
              :class="dayClass(d)"
              :aria-label="`${monthLabel}${d}日${signedDays.has(d) ? '已报到' : ''}`"
            >
              <span class="n">{{ d }}</span>
            </div>
          </div>
        </div>
      </section>

      <!-- 报到按钮 -->
      <button
        ref="checkinBtn"
        type="button"
        class="checkin-btn"
        :disabled="!(statusError && !status) && (!canCheckin || checking)"
        @click="onMainBtnClick"
      >
        <span v-if="checkinBtnIcon" aria-hidden="true">{{ checkinBtnIcon }}</span>{{ checkinBtnText }}
      </button>

      <!-- 奖励说明 -->
      <div class="reward" v-if="status && status.enabled">
        <span aria-hidden="true">🎁</span> <b>报到奖励</b>：能量 +{{ status.base_points }}⚡<br>
        <span aria-hidden="true">🔥</span> <b>连报奖励</b>：每天多 +{{ status.streak_bonus }}⚡（上限 +{{ status.streak_max_bonus }}⚡）
      </div>
    </div>
    <PanelTabbar />
    <!-- 盖章动画 -->
    <div ref="sealPop" class="seal-pop" aria-hidden="true">到</div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import PanelTabbar from '@/components/panel/PanelTabbar.vue'
import StaminaBar from '@/components/game/StaminaBar.vue'
import { staminaText } from '@/config/stamina'
import { checkinApi, pointsApi, type CheckinStatus } from '@/api/economy'
import { useToast } from '@/composables/useToast'
import { humanizeError } from '@/composables/usePanelApi'

const now = new Date()
const year = ref(now.getFullYear())
const month = ref(now.getMonth())
const todayNum = ref(now.getDate())

const status = ref<CheckinStatus | null>(null)
const statusLoading = ref(true)
const statusError = ref('')
const checking = ref(false)
const sealPop = ref<HTMLElement | null>(null)
const checkinBtn = ref<HTMLButtonElement | null>(null)
const staminaBar = ref<InstanceType<typeof StaminaBar> | null>(null)

/** 本月报到过的日期（来自报到流水） */
const signedDays = ref<Set<number>>(new Set())

const daysInMonth = computed(() => new Date(year.value, month.value + 1, 0).getDate())
const monthLabel = computed(() => `${month.value + 1}月`)
const signedCount = computed(() => signedDays.value.size)

const canCheckin = computed(
  () => !!status.value && status.value.enabled && !status.value.checked_today,
)
const checkinBtnIcon = computed(() => {
  if (statusLoading.value) return ''
  if (statusError.value && !status.value) return ''
  if (!status.value) return '📋'
  if (!status.value.enabled) return ''
  if (status.value.checked_today) return '✅'
  return '📋'
})
const checkinBtnText = computed(() => {
  if (statusLoading.value) return '加载中…'
  if (statusError.value && !status.value) return '重试'
  if (checking.value) return '盖章中'
  if (!status.value) return '报 到'
  if (!status.value.enabled) return '报到未开启'
  if (status.value.checked_today) return '今日已报到'
  return '报 到'
})

/** 主按钮统一入口：错误态点"重试"走 load()，其余走报到 */
function onMainBtnClick() {
  if (statusError.value && !status.value) {
    void load()
  } else {
    void doCheckin()
  }
}

function dayClass(d: number): string {
  if (signedDays.value.has(d)) return 'done'
  if (d === todayNum.value) return 'today'
  if (d > todayNum.value) return 'future'
  return ''
}

/** 重载失败时保留旧 status，加"数据可能过期"小字提示 */
const statusStale = ref(false)
/** 请求序号：旧响应直接丢弃，不碰状态 */
let loadSeq = 0

async function load() {
  const seq = ++loadSeq
  statusLoading.value = true
  statusError.value = ''
  try {
    const st = await checkinApi.status()
    if (seq !== loadSeq) return

    // R48A-4：流水失败只降级，不清空旧 signedDays（区分"成功返回空"与"请求失败"）
    let log: Awaited<ReturnType<typeof pointsApi.log>> | null = null
    try {
      log = await pointsApi.log({ limit: 100, type_filter: 'checkin' })
    } catch (e) {
      console.error('[checkin] 报到流水加载失败，已保留旧数据', e)
    }
    if (seq !== loadSeq) return

    status.value = st
    statusStale.value = false
    if (log) {
      const days = new Set<number>()
      for (const l of log.logs) {
        if (!l.created_at) continue
        const d = new Date(l.created_at)
        if (d.getFullYear() === year.value && d.getMonth() === month.value) {
          days.add(d.getDate())
        }
      }
      // 今天已报到时，确保今天也在集合里（流水可能有延迟）
      if (st.checked_today) days.add(todayNum.value)
      signedDays.value = days
    }
  } catch (e) {
    if (seq !== loadSeq) return
    statusError.value = humanizeError(e)
    if (status.value) statusStale.value = true
  } finally {
    if (seq === loadSeq) statusLoading.value = false
  }
}

async function doCheckin() {
  if (!canCheckin.value || checking.value) return
  checking.value = true
  try {
    const r = await checkinApi.doCheckin()
    if (status.value) {
      status.value.checked_today = true
      status.value.streak = r.streak
      status.value.points = r.balance
    }
    signedDays.value = new Set(signedDays.value).add(todayNum.value)
    // 盖章动画
    const pop = sealPop.value
    if (pop) {
      pop.classList.remove('go')
      void pop.offsetWidth
      pop.classList.add('go')
    }
    // 能量飞入（R43：付费用户无积分奖励，points_awarded=0 时不飞"+0⚡"，免得误导）
    if (r.points_awarded > 0) flyEnergy(`+${r.points_awarded}⚡`, checkinBtn.value)
    // 报到成功的反馈不许被体力刷新绑架：先定文案、只弹一次。
    // 体力条对付费用户不可见、刷新失败时 vitality 回 null，
    // 旧代码此时直接不弹 toast，用户以为报到没成功（静默成功）。
    const before = staminaBar.value?.current() ?? null
    let doneMsg = '报到成功'
    try {
      const v = await staminaBar.value?.refresh()
      // 只有体力数值真的涨了才说"体力已恢复"，体力已满/不可见时只说"报到成功"
      if (before !== null && v !== null && v !== undefined && v > before) {
        doneMsg = staminaText.checkinRecovered
      }
    } catch {
      /* 体力刷新失败不打扰主流程 */
    }
    useToast().success(doneMsg)
  } catch (e) {
    // 提交失败走 toast，不污染报到簿的 statusError
    useToast().error(humanizeError(e))
  } finally {
    checking.value = false
  }
}

let flyTimer = 0

function flyEnergy(text: string, fromEl?: HTMLElement | null) {
  // 减少动态偏好：跳过飞行动画
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
  const fly = document.createElement('div')
  fly.className = 'energy-fly go'
  fly.textContent = text
  if (fromEl) {
    const rect = fromEl.getBoundingClientRect()
    fly.style.left = `${rect.left + rect.width / 2}px`
    fly.style.top = `${rect.top}px`
  } else {
    fly.style.left = '60%'
    fly.style.top = '55%'
  }
  document.body.appendChild(fly)
  // R48A-46：记下定时器 id，卸载时清理
  flyTimer = window.setTimeout(() => fly.remove(), 1300)
}

/** 跨午夜日期刷新：每分钟核对一次，翻天了就更新今天并重载 */
let dateTimer = 0
onMounted(() => {
  load()
  dateTimer = window.setInterval(() => {
    const n = new Date()
    if (n.getDate() !== todayNum.value || n.getMonth() !== month.value || n.getFullYear() !== year.value) {
      year.value = n.getFullYear()
      month.value = n.getMonth()
      todayNum.value = n.getDate()
      void load()
    }
  }, 60_000)
})
onUnmounted(() => {
  window.clearInterval(dateTimer)
  window.clearTimeout(flyTimer)
})
</script>

<style scoped>
/* 报到簿：设计稿 game-checkin.html 原值 */
.book {
  background: var(--paper-card);
  border: 1.5px solid var(--line);
  border-radius: var(--paper-r-lg, 12px);
  padding: 16px;
  box-shadow: 0 2px 8px rgba(42, 38, 32, 0.06);
}
.book-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}
.book-head b { font-size: 15px; }
.book-head span { font-size: 12px; color: var(--ink2); }
.cal {
  display: grid;
  grid-template-columns: repeat(7, 1fr);
  gap: 6px;
}
.day {
  aspect-ratio: 1;
  border-radius: var(--paper-r-sm, 8px);
  background: var(--paper2);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  color: var(--ink2);
  position: relative;
  border: 1px solid transparent;
}
.day .n { font-weight: 700; font-size: 12px; }
.day.done {
  /* R48：原值 #F1F4EC，改用主题变量拼出近似纸绿色 */
  background: color-mix(in srgb, var(--green) 8%, var(--paper-card));
  border-color: var(--green);
}
.day.done .n { color: var(--green); }
.day.done::after {
  content: "到";
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 20px;
  font-weight: 900;
  color: var(--red);
  opacity: 0.28;
  transform: rotate(-12deg);
  border: 2px solid var(--red);
  border-radius: var(--paper-r-sm, 8px);
  margin: 2px;
}
.day.today {
  border: 2px dashed var(--red);
}
/* 脉冲光晕只用 transform/opacity（60fps）：::after 做扩散圆环，不刷 box-shadow */
.day.today::after {
  content: "";
  position: absolute;
  inset: -2px;
  border: 2px solid rgba(217, 72, 43, 0.35);
  border-radius: var(--paper-r-sm, 8px);
  animation: pulse 1.6s infinite;
  pointer-events: none;
}
@keyframes pulse {
  0%, 100% { transform: scale(1); opacity: 0.7; }
  50% { transform: scale(1.18); opacity: 0; }
}
.day.future { opacity: 0.45; }

/* 报到按钮：设计稿原值 */
.checkin-btn {
  display: block;
  width: calc(100% - 40px);
  margin: 18px 20px 0;
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
  -webkit-tap-highlight-color: transparent;
}
.checkin-btn:active { transform: translateY(3px); box-shadow: none; }
.checkin-btn:disabled { background: var(--ink2); box-shadow: none; cursor: default; }

/* 奖励说明 */
.reward {
  margin: 14px 20px 0;
  background: var(--paper2);
  border: 1px dashed var(--gold);
  border-radius: var(--paper-r-md, 10px);
  padding: 12px 14px;
  font-size: 12px;
  line-height: 1.9;
}
.reward b { color: var(--gold); }

/* 盖章动画：设计稿原值 */
.seal-pop {
  position: fixed;
  top: 40%;
  left: 50%;
  transform: translate(-50%, -50%) scale(3);
  font-size: 90px;
  font-weight: 900;
  color: var(--red);
  opacity: 0;
  pointer-events: none;
  z-index: 99;
  border: 6px solid var(--red);
  border-radius: var(--paper-r-xl, 16px);
  padding: 10px 26px;
  background: rgba(246, 241, 231, 0.92);
}
.seal-pop.go { animation: sealpop 0.8s ease-out forwards; }
@keyframes sealpop {
  0% { opacity: 0; transform: translate(-50%, -50%) scale(3) rotate(-18deg); }
  40% { opacity: 1; transform: translate(-50%, -50%) scale(1) rotate(-12deg); }
  70% { transform: translate(-50%, -50%) scale(1.05) rotate(-12deg); }
  100% { opacity: 0; transform: translate(-50%, -50%) scale(0.9) rotate(-12deg); }
}
/* R48A-11：飞字元素挂在 document.body 上，scoped 够不着，提为全局 */
:global(.energy-fly) {
  position: fixed;
  font-size: 18px;
  font-weight: 800;
  color: var(--gold);
  pointer-events: none;
  z-index: 99;
  opacity: 0;
}
:global(.energy-fly.go) { animation: efly 1.2s ease-out forwards; }
@keyframes efly {
  0% { opacity: 1; transform: translate(0, 0); }
  100% { opacity: 0; transform: translate(60px, -120px); }
}

/* R48A-39：骨架修饰类（替代内联 style） */
.sk-fire { height: 32px; width: 60%; margin-top: 10px; }
.sk-book { height: 220px; }
/* R48A-13：数据可能过期的小字提示 */
.v4-stale { font-size: 11px; color: var(--ink2); }
/* R27：减少动态偏好下装饰动画停掉（本地守卫） */
@media (prefers-reduced-motion: reduce) {
  .day.today::after { animation: none; }
  /* R48A-28：停掉动画时补静态终态，别停在 opacity:0 不可见 */
  .seal-pop.go { animation: none; opacity: 1; transform: translate(-50%, -50%) scale(1) rotate(-12deg); }
  :global(.energy-fly.go) { animation: none; opacity: 1; transform: translate(60px, -120px); }
  .checkin-btn:active { transform: none; }
}
</style>
