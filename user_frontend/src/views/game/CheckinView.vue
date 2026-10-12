<template>
  <div class="v4-scope">
    <div class="v4-page">
      <!-- 日志头 -->
      <header class="v4-loghead">
        <span class="v4-stamp">营地 · 报到处</span>
        <h1>📋 每日报到</h1>
        <p>探险家每天到营地报到，盖章打卡，领能量。</p>
        <div v-if="status" class="v4-fire">
          🔥 已连报 <b>{{ status.streak }}</b> 天
          <span v-if="status.streak >= 7"> · 🏅 金章达成</span>
          <span v-else-if="!status.checked_today"> · 今天还没报到</span>
        </div>
        <div v-else-if="statusLoading" class="v4-skeleton" style="height:32px;width:60%;margin-top:10px"></div>
      </header>

      <!-- 签到簿 -->
      <section class="v4-sec">
        <div class="v4-sec-title">📖 签到簿 · {{ monthLabel }}</div>
        <div class="v4-sec-sub">红章=已到 · 今天那格闪烁，点"报到"盖章</div>
        <div v-if="statusLoading" class="v4-skeleton" style="height:220px"></div>
        <div v-else-if="statusError" class="v4-err">签到簿加载失败：{{ statusError }}<br><button class="v4-retry" @click="load">重试</button></div>
        <div v-else class="book">
          <div class="book-head">
            <b>{{ year }}年{{ month + 1 }}月</b>
            <span>本月已报 {{ signedCount }} 天</span>
          </div>
          <div class="cal">
            <div
              v-for="d in daysInMonth"
              :key="d"
              class="day"
              :class="dayClass(d)"
              :aria-label="`${month + 1}月${d}日${signedDays.has(d) ? '已报到' : ''}`"
            >
              <span class="n">{{ d }}</span>
            </div>
          </div>
        </div>
      </section>

      <!-- 报到按钮 -->
      <button
        class="checkin-btn"
        :disabled="!canCheckin || checking"
        @click="doCheckin"
      >
        {{ checkinBtnText }}
      </button>

      <!-- 奖励说明 -->
      <div class="reward" v-if="status">
        🎁 <b>报到奖励</b>：能量 +{{ status.base_points }}⚡<br>
        🔥 <b>连报奖励</b>：每天多 +{{ status.streak_bonus }}⚡（上限 +{{ status.streak_max_bonus }}⚡）
      </div>
    </div>
    <PanelTabbar />
    <!-- 盖章动画 -->
    <div ref="sealPop" class="seal-pop" aria-hidden="true">到</div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import PanelTabbar from '@/components/panel/PanelTabbar.vue'
import { checkinApi, pointsApi, type CheckinStatus } from '@/api/economy'
import { useToast } from '@/composables/useToast'
import { humanizeError } from '@/composables/usePanelApi'

const now = new Date()
const year = ref(now.getFullYear())
const month = ref(now.getMonth())
const todayNum = now.getDate()

const status = ref<CheckinStatus | null>(null)
const statusLoading = ref(true)
const statusError = ref('')
const checking = ref(false)
const sealPop = ref<HTMLElement | null>(null)

/** 本月签到过的日期（来自签到流水） */
const signedDays = ref<Set<number>>(new Set())

const daysInMonth = computed(() => new Date(year.value, month.value + 1, 0).getDate())
const monthLabel = computed(() => `${month.value + 1}月`)
const signedCount = computed(() => signedDays.value.size)

const canCheckin = computed(
  () => !!status.value && status.value.enabled && !status.value.checked_today,
)
const checkinBtnText = computed(() => {
  if (checking.value) return '盖章中…'
  if (!status.value) return '📋 报 到'
  if (!status.value.enabled) return '报到未开启'
  if (status.value.checked_today) return '✅ 今日已报到'
  return '📋 报 到'
})

function dayClass(d: number): string {
  if (signedDays.value.has(d)) return 'done'
  if (d === todayNum) return 'today'
  if (d > todayNum) return 'future'
  return ''
}

async function load() {
  statusLoading.value = true
  statusError.value = ''
  try {
    const [st, log] = await Promise.all([
      checkinApi.status(),
      pointsApi.log({ limit: 100, type_filter: 'checkin' }).catch(() => ({ logs: [] as { created_at: string | null }[] })),
    ])
    status.value = st
    const days = new Set<number>()
    for (const l of log.logs) {
      if (!l.created_at) continue
      const d = new Date(l.created_at)
      if (d.getFullYear() === year.value && d.getMonth() === month.value) {
        days.add(d.getDate())
      }
    }
    // 今天已签到时，确保今天也在集合里（流水可能有延迟）
    if (st.checked_today) days.add(todayNum)
    signedDays.value = days
  } catch (e) {
    statusError.value = humanizeError(e)
  } finally {
    statusLoading.value = false
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
    signedDays.value = new Set(signedDays.value).add(todayNum)
    // 盖章动画
    const pop = sealPop.value
    if (pop) {
      pop.classList.remove('go')
      void pop.offsetWidth
      pop.classList.add('go')
    }
    // 能量飞入
    flyEnergy(`+${r.points_awarded}⚡`)
  } catch (e) {
    // 提交失败走 toast，不污染签到簿的 statusError
    useToast().error(humanizeError(e))
  } finally {
    checking.value = false
  }
}

function flyEnergy(text: string) {
  const fly = document.createElement('div')
  fly.className = 'energy-fly go'
  fly.textContent = text
  fly.style.left = '60%'
  fly.style.top = '55%'
  document.body.appendChild(fly)
  window.setTimeout(() => fly.remove(), 1300)
}

onMounted(load)
</script>

<style scoped>
/* 签到簿：设计稿 game-checkin.html 原值 */
.book {
  background: #FFFDF6;
  border: 1.5px solid var(--line);
  border-radius: 12px;
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
  border-radius: 8px;
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
  background: #F1F4EC;
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
  border-radius: 8px;
  margin: 2px;
}
.day.today {
  border: 2px dashed var(--red);
  animation: pulse 1.6s infinite;
}
@keyframes pulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(217, 72, 43, 0.25); }
  50% { box-shadow: 0 0 0 6px rgba(217, 72, 43, 0); }
}
.day.future { opacity: 0.45; }

/* 报到按钮：设计稿原值 */
.checkin-btn {
  display: block;
  width: calc(100% - 40px);
  margin: 18px 20px 0;
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
.checkin-btn:active { transform: translateY(2px); box-shadow: none; }
.checkin-btn:disabled { background: var(--ink2); box-shadow: none; cursor: default; }

/* 奖励说明 */
.reward {
  margin: 14px 20px 0;
  background: var(--paper2);
  border: 1px dashed var(--gold);
  border-radius: 10px;
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
  border-radius: 16px;
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
.energy-fly {
  position: fixed;
  font-size: 18px;
  font-weight: 800;
  color: var(--gold);
  pointer-events: none;
  z-index: 99;
  opacity: 0;
}
.energy-fly.go { animation: efly 1.2s ease-out forwards; }
@keyframes efly {
  0% { opacity: 1; transform: translate(0, 0); }
  100% { opacity: 0; transform: translate(60px, -120px); }
}
</style>
