<template>
  <div class="v4-scope">
    <div class="v4-page">
      <!-- 日志头 -->
      <header class="v4-loghead">
        <span class="v4-stamp">营地 · 探险地图</span>
        <h1>🗺️ 探险预告</h1>
        <p>新抵达的大陆，先睹为快。点"想去"加入探险计划。</p>
      </header>

      <!-- 时间线 -->
      <section class="v4-sec">
        <div class="v4-sec-title">🧭 最近抵达</div>
        <div class="v4-sec-sub">藏宝图=已确认到货 · 点卡片去看看</div>
        <div v-if="loading" class="v4-skeleton" style="height:220px"></div>
        <div v-else-if="error" class="v4-err">探险地图加载失败：{{ error }}<br><button class="v4-retry" @click="load">重试</button></div>
        <div v-else class="timeline">
          <div
            v-for="(day, di) in days"
            :key="day.date"
            class="tday"
            :class="{ hot: di === 0 }"
          >
            <div class="tdate">{{ dayLabel(day.date, di) }}</div>
            <div class="cards">
              <div
                v-for="item in day.items.slice(0, expanded.has(day.date) ? day.items.length : 3)"
                :key="item.Id"
                class="treasure"
              >
                <div class="map">{{ typeIcon(item.Type) }}</div>
                <div class="info">
                  <b>{{ item.Name }}</b>
                  <span>{{ item.LibraryName || '' }} · {{ typeLabel(item.Type) }}</span>
                </div>
                <button
                  class="want"
                  :class="{ on: plan.has(item.Id) }"
                  @click="togglePlan(item.Id)"
                >
                  <span class="lbl">{{ plan.has(item.Id) ? '已加入 ✓' : '想去' }}</span>
                </button>
              </div>
              <button v-if="day.count > 3" class="more" @click="toggleExpand(day.date)">{{ expanded.has(day.date) ? '收起 ↑' : '还有 ' + (Math.min(day.count, day.items.length) - 3) + ' 件，点我看全部 →' }}</button>
              <div v-if="day.items.length === 0" class="empty-day">这天营地很安静，没有新大陆</div>
            </div>
          </div>

          <!-- 未来：保密新大陆（悬念占位，非虚假数据） -->
          <div class="tday future">
            <div class="tdate">未来 · 保密中 <span class="countdown">???</span></div>
            <div class="cards">
              <div class="treasure mystery">
                <div class="map">🌫️</div>
                <div class="info">
                  <b>? ? ?</b>
                  <span>神秘新大陆 · 到日子自动揭晓</span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>

      <!-- 探险计划条 -->
      <div v-if="plan.size > 0" class="plan-bar">
        <span>🎒 探险计划 <b>{{ plan.size }}</b> 个目的地</span>
      </div>
    </div>
    <PanelTabbar />
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import PanelTabbar from '@/components/panel/PanelTabbar.vue'
import { fetchCalendar, type CalendarDay } from '@/api/calendar'
import { humanizeError } from '@/composables/usePanelApi'

const days = ref<CalendarDay[]>([])
const loading = ref(true)
const error = ref('')
/** 探险计划：本地 Set（v1 不落库） */
const plan = ref<Set<string>>(new Set())

function fmt(d: Date): string {
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${d.getFullYear()}-${m}-${day}`
}

function dayLabel(date: string, idx: number): string {
  const [, m, d] = date.split('-').map(Number)
  if (idx === 0) return `今天 · ${m}月${d}日`
  if (idx === 1) return `昨天 · ${m}月${d}日`
  return `${m}月${d}日`
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

/** 展开某天全部藏宝（"还有 N 件"按钮用） */
const expanded = ref<Set<string>>(new Set())
function toggleExpand(date: string) {
  const next = new Set(expanded.value)
  if (next.has(date)) next.delete(date)
  else next.add(date)
  expanded.value = next
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const end = new Date()
    const start = new Date()
    start.setDate(start.getDate() - 6)
    const res = await fetchCalendar({ start: fmt(start), end: fmt(end) })
    // 最新在前
    days.value = [...res.days].reverse()
  } catch (e) {
    error.value = humanizeError(e)
  } finally {
    loading.value = false
  }
}

onMounted(load)
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
  border-radius: 50%;
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
  background: #FFFDF6;
  border: 1.5px solid var(--line);
  border-radius: 10px;
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
  /* 透明 padding + 负 margin：视觉药丸不变，点击区纵向扩大到 44px（R22 LeverSwitch 同款手法） */
  background: transparent;
  border: none;
  font-size: 12px;
  font-weight: 700;
  padding: 13px 12px;
  margin: -6px 0;
  cursor: pointer;
  color: var(--ink);
}
/* 视觉药丸画在 ::before 上，随 padding 偏移（上下各 6px），与原来像素一致 */
.treasure .want::before {
  content: "";
  position: absolute;
  top: 6px; right: 0; bottom: 6px; left: 0;
  background: var(--paper2);
  border: 1.5px solid var(--ink);
  border-radius: 16px;
}
.treasure .want .lbl { position: relative; }
.treasure .want.on { color: #fff; }
.treasure .want.on::before { background: var(--ink); }
.treasure.mystery { background: var(--paper2); border-style: dashed; }
.treasure.mystery .map { filter: grayscale(1) blur(1px); }
.treasure.mystery .info b { letter-spacing: 4px; color: var(--ink2); }
.countdown {
  display: inline-block;
  background: var(--gold);
  color: #fff;
  font-size: 10px;
  font-weight: 800;
  padding: 2px 8px;
  border-radius: 10px;
  margin-left: 6px;
  vertical-align: middle;
}
.more {
  font-size: 11px; color: var(--ink2);
  background: none; border: none; cursor: pointer; text-align: left;
  /* 透明 padding + 负 margin：文案行视觉不变，点击区纵向 44px */
  padding: 14px 0; margin: -12px 0;
}
.empty-day { font-size: 11px; color: var(--ink2); padding: 2px 0; }
.plan-bar {
  position: sticky;
  bottom: 90px;
  margin: 16px 20px 0;
  background: var(--ink);
  color: #F6F1E7;
  border-radius: 12px;
  padding: 12px 16px;
  font-size: 13px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  box-shadow: 0 4px 14px rgba(42, 38, 32, 0.25);
}
.plan-bar b { color: #FFB25E; }
</style>
