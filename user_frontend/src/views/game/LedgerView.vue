<template>
  <div class="v4-scope">
    <div class="v4-page">
      <!-- 日志头 -->
      <header class="v4-loghead">
        <span class="v4-stamp">{{ facilityStamp('ledger') }}</span>
        <h1>📒 能量账本</h1>
        <p>探险家的随身账本 —— 能量从哪来，到哪去。</p>
      </header>

      <!-- 燃料表 -->
      <section class="v4-sec" aria-label="燃料表">
        <div v-if="loading && !balanceLoaded" class="v4-skeleton" style="height:150px;border-radius:14px"></div>
        <div v-else-if="error && !balanceLoaded" class="v4-err">
          燃料表加载失败：{{ error }}<br>
          <button class="v4-retry" @click="reload">重试</button>
        </div>
        <div v-else class="fuel">
          <div class="fuel-lbl">⚡ 当前能量 · 燃料表</div>
          <div class="fuel-num"><small>⚡</small> {{ fmtNum(balance) }}</div>
          <!-- R27：非「全部」筛选时后端 total 不可信（是未筛选总数），数字去掉，不加后端接口 -->
          <span v-if="todayIncome > 0" class="fuel-today">{{ !hasMore && filter === 'all' ? `今日 +${fmtNum(todayIncome)} ⚡` : '今日有进账 ⚡' }}</span>
        </div>
      </section>

      <!-- 筛选 -->
      <section class="v4-sec" aria-label="筛选">
        <div class="filter" role="group" aria-label="收支筛选">
          <button
            v-for="f in filters"
            :key="f.key"
            type="button"
            class="chip"
            :class="{ on: filter === f.key }"
            :aria-pressed="filter === f.key"
            @click="filter = f.key"
          >{{ f.label }}</button>
        </div>
      </section>

      <!-- 账本 -->
      <section class="v4-sec" aria-label="账本流水">
        <div v-if="loading && !logs.length" class="v4-skeleton" style="height:220px"></div>
        <div v-else-if="error && !logs.length" class="v4-err">
          账本加载失败：{{ error }}<br>
          <button class="v4-retry" @click="reload">重试</button>
        </div>
        <div v-else-if="!filteredGroups.length" class="v4-empty">
          这一栏空空的 —— 去探索片库大陆，赚点能量回来吧 🧭
        </div>
        <template v-else>
          <div v-for="g in filteredGroups" :key="g.key" class="day-group">
            <div class="v4-sec-title">{{ g.title }}</div>
            <div class="v4-sec-sub">{{ g.sub }}</div>
            <div
              v-for="l in g.rows"
              :key="l.id"
              class="row"
              :class="isIncome(l) ? 'in' : 'out'"
            >
              <div class="ic" aria-hidden="true">{{ gameMeta(l.type).icon }}</div>
              <div class="mid">
                <div class="t">{{ l.description || gameMeta(l.type).title }}</div>
                <div class="d">{{ fmtTime(l.created_at) }}</div>
              </div>
              <div class="amt">{{ isIncome(l) ? '+' : '−' }}{{ fmtNum(Math.abs(l.amount)) }} ⚡</div>
            </div>
          </div>

          <!-- 加载更多 -->
          <button
            v-if="hasMore"
            type="button"
            class="load-more"
            :disabled="loadingMore"
            @click="loadMore"
          >
            {{ loadingMore ? '翻页中' : (filter === 'all' ? `加载更多（还剩 ${total - logs.length} 笔）` : '加载更多') }}
          </button>
          <div v-else-if="logs.length" class="v4-sec-sub" style="text-align:center;margin-top:14px">
            —— 账本到这里就记满了 ——
          </div>
        </template>
      </section>
    </div>
    <PanelTabbar />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import PanelTabbar from '@/components/panel/PanelTabbar.vue'
import { facilityStamp } from '@/config/game'
import { pointsApi, type PointsLogEntry } from '@/api/economy'
import { humanizeError } from '@/composables/usePanelApi'
import { useToast } from '@/composables/useToast'

const toast = useToast()

/**
 * 类型元数据：与 WalletView.vue 的 TYPE_META 完全一致（值保持原样）。
 * income 为准判断进/出账；未知 type 兜底显示原值、按进账处理。
 */
const TYPE_META: Record<string, { label: string; income: boolean }> = {
  checkin: { label: '报到', income: true },
  invite: { label: '邀请奖励', income: true },
  invitee: { label: '受邀奖励', income: true },
  rebate: { label: '充值返利', income: true },
  exchange: { label: '兑换码', income: true },
  recharge: { label: '充值', income: true },
  admin_grant: { label: '管理员发放', income: true },
  admin_deduct: { label: '管理员扣除', income: false },
  transfer_out: { label: '转账转出', income: false },
  transfer_in: { label: '转账转入', income: true },
  transfer_fee: { label: '转账手续费', income: false },
}
function typeMeta(t: string) {
  return TYPE_META[t] || { label: '其他', income: true }
}

/** 世界观映射：游戏化标签（值按任务约定） */
const GAME_META: Record<string, { icon: string; title: string }> = {
  checkin: { icon: '📋', title: '每日报到' },
  invite: { icon: '🤝', title: '结伴同行奖励' },
  invitee: { icon: '🎁', title: '受邀见面礼' },
  rebate: { icon: '💰', title: '充值返利' },
  exchange: { icon: '🎟️', title: '兑换码' },
  recharge: { icon: '⚡', title: '能量充值' },
  admin_grant: { icon: '📢', title: '营地发放' },
  admin_deduct: { icon: '📢', title: '营地扣除' },
  transfer_out: { icon: '💸', title: '转账转出' },
  transfer_in: { icon: '💰', title: '转账转入' },
  transfer_fee: { icon: '🧾', title: '转账手续费' },
}
function gameMeta(t: string) {
  return GAME_META[t] || { icon: '📦', title: typeMeta(t).label }
}

const isIncome = (l: PointsLogEntry) => typeMeta(l.type).income

const PAGE_SIZE = 30
type FilterKey = 'all' | 'in' | 'out'
const filters: { key: FilterKey; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'in', label: '进账' },
  { key: 'out', label: '出账' },
]

const balance = ref(0)
const balanceLoaded = ref(false)
const logs = ref<PointsLogEntry[]>([])
const total = ref(0)
const loading = ref(true)
const loadingMore = ref(false)
const error = ref('')
const filter = ref<FilterKey>('all')

function fmtNum(n: number | null | undefined) {
  if (n == null) return '—'
  return Math.round(n).toLocaleString()
}

function fmtTime(iso?: string | null) {
  if (!iso) return '—'
  const d = new Date(iso)
  const m = d.getMonth() + 1
  const day = d.getDate()
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  return `${m}-${day} ${hh}:${mm}`
}

/** 本地日期键 YYYY-M-D */
function dayKey(d: Date) {
  return `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`
}

async function load(reset = true) {
  if (reset) {
    loading.value = true
    error.value = ''
  }
  try {
    const offset = reset ? 0 : logs.value.length
    const res = await pointsApi.log({ limit: PAGE_SIZE, offset })
    balance.value = res.balance ?? 0
    balanceLoaded.value = true
    total.value = res.total ?? 0
    logs.value = reset ? (res.logs || []) : [...logs.value, ...(res.logs || [])]
  } catch (e) {
    if (reset) {
      error.value = humanizeError(e)
      // P2-5：刷新失败但有旧数据时，模板不显示错误态（只在无数据时显示），弹人话提示
      if (balanceLoaded.value || logs.value.length) {
        toast.error('刷新失败，显示的是之前的数据')
      }
    } else {
      // 加载更多失败：静默吞掉会让用户以为到底了，弹人话提示
      toast.error(humanizeError(e))
    }
  } finally {
    if (reset) loading.value = false
  }
}

async function reload() {
  await load(true)
}

async function loadMore() {
  if (loadingMore.value) return
  loadingMore.value = true
  try {
    await load(false)
  } finally {
    loadingMore.value = false
  }
}

const hasMore = computed(() => logs.value.length < total.value)

/** 今日进账合计（按本地日期，从已加载流水汇总；>0 才显示徽章） */
const todayIncome = computed(() => {
  const now = new Date()
  const key = dayKey(now)
  let sum = 0
  for (const l of logs.value) {
    if (!l.created_at || !isIncome(l)) continue
    if (dayKey(new Date(l.created_at)) === key) sum += Math.abs(l.amount)
  }
  return Math.round(sum)
})

interface DayGroup {
  key: string
  title: string
  sub: string
  rows: PointsLogEntry[]
}

/** 按本地日期分组：今天 / 昨天 / 更早（数据驱动） */
const filteredGroups = computed<DayGroup[]>(() => {
  const rows = logs.value.filter((l) => {
    if (filter.value === 'in') return isIncome(l)
    if (filter.value === 'out') return !isIncome(l)
    return true
  })
  if (!rows.length) return []

  const now = new Date()
  const todayK = dayKey(now)
  const yes = new Date(now)
  yes.setDate(yes.getDate() - 1)
  const yesK = dayKey(yes)

  const groups = new Map<string, DayGroup>()
  for (const l of rows) {
    let key: string
    let title: string
    let sub: string
    if (l.created_at) {
      const d = new Date(l.created_at)
      const k = dayKey(d)
      const md = `${d.getMonth() + 1}.${d.getDate()}`
      if (k === todayK) {
        key = 'today'; title = `📅 今天 · ${md}`; sub = '账本上还热乎的墨迹'
      } else if (k === yesK) {
        key = 'yesterday'; title = `📅 昨天 · ${md}`; sub = '昨天走过的路'
      } else {
        key = 'older'; title = '📅 更早'; sub = '更早的足迹'
      }
    } else {
      key = 'older'; title = '📅 更早'; sub = '更早的足迹'
    }
    if (!groups.has(key)) groups.set(key, { key, title, sub, rows: [] })
    groups.get(key)!.rows.push(l)
  }

  // 今天 > 昨天 > 更早
  const order: DayGroup[] = []
  if (groups.has('today')) order.push(groups.get('today')!)
  if (groups.has('yesterday')) order.push(groups.get('yesterday')!)
  if (groups.has('older')) order.push(groups.get('older')!)
  return order
})

/** 跨午夜日期刷新：每分钟核对一次，翻天了就重载（今天/昨天分组随之更新） */
const dayStamp = ref(dayKey(new Date()))
let dateTimer = 0
onMounted(() => {
  load(true)
  dateTimer = window.setInterval(() => {
    if (dayKey(new Date()) !== dayStamp.value) {
      dayStamp.value = dayKey(new Date())
      void load(true)
    }
  }, 60_000)
})
onUnmounted(() => window.clearInterval(dateTimer))
</script>

<style scoped>
/* 燃料表：设计稿 game-ledger.html 的深色卡片原值 */
.fuel {
  background: var(--ink);
  border-radius: 14px;
  padding: 20px;
  color: #EDE6D6;
  position: relative;
  overflow: hidden;
}
.fuel::before {
  content: "";
  position: absolute;
  inset: 0;
  background: radial-gradient(circle at 85% 15%, rgba(201, 155, 63, 0.22), transparent 45%);
  pointer-events: none;
}
.fuel-lbl {
  font-size: 11px;
  letter-spacing: 3px;
  color: var(--pulse-green, #9BE08A);
  position: relative;
}
.fuel-num {
  font-size: 52px;
  font-weight: 800;
  margin: 6px 0 2px;
  position: relative;
  letter-spacing: 1px;
  line-height: 1.1;
}
.fuel-num small {
  font-size: 22px;
  font-weight: 700;
  color: var(--ember, #FFB25E);
}
.fuel-today {
  display: inline-block;
  background: rgba(155, 224, 138, 0.15);
  border: 1px solid rgba(155, 224, 138, 0.5);
  color: var(--pulse-green, #9BE08A);
  font-size: 12px;
  font-weight: 700;
  padding: 4px 12px;
  border-radius: var(--paper-r-xl, 16px);
  margin-top: 8px;
  position: relative;
}

/* 筛选小标签：设计稿 game-ledger.html 原值，触达区 ≥44px */
.filter {
  display: flex;
  gap: 10px;
}
.chip {
  flex: 1;
  text-align: center;
  font-size: 13px;
  font-weight: 700;
  min-height: 44px;
  border-radius: var(--paper-r-2xl, 20px);
  border: 1.5px solid var(--ink2);
  color: var(--ink2);
  background: transparent;
  cursor: pointer;
  transition: background-color 0.15s, color 0.15s, border-color 0.15s, transform 0.15s;
}
.chip.on {
  background: var(--ink);
  border-color: var(--ink);
  color: var(--paper, #F6F1E7);
}
.chip:active { transform: scale(0.95); }

/* 分组 */
.day-group { margin-bottom: 6px; }

/* 账本行：设计稿 game-ledger.html 原值 */
.row {
  display: flex;
  align-items: center;
  gap: 12px;
  background: var(--paper-card);
  border: 1px solid var(--line);
  border-radius: var(--paper-r-md, 10px);
  padding: 13px 14px;
  margin-bottom: 10px;
}
.row .ic {
  width: 38px;
  height: 38px;
  flex: none;
  border-radius: var(--paper-r-full, 50%);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 19px;
}
.row.in .ic { background: #E9F1E6; color: var(--green); }
.row.out .ic { background: #F9E7E1; color: var(--red); }
.row .mid { flex: 1; min-width: 0; }
.row .t {
  font-size: 13.5px;
  font-weight: 700;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.row .d { font-size: 11px; color: var(--ink2); margin-top: 3px; }
.row .amt { font-size: 16px; font-weight: 800; white-space: nowrap; }
.row.in .amt { color: var(--green); }
.row.out .amt { color: var(--red); }

/* 加载更多：触达区 ≥44px */
.load-more {
  display: block;
  width: calc(100% - 40px);
  margin: 16px 20px 0;
  min-height: 44px;
  border-radius: var(--paper-r-lg, 12px);
  border: 1.5px solid var(--line);
  background: var(--paper-card);
  color: var(--ink);
  font-size: 14px;
  font-weight: 700;
  cursor: pointer;
}
.load-more:active { transform: scale(0.97); }
.load-more:disabled { opacity: 0.6; cursor: default; }

/* 键盘可达性：按钮获得焦点时给出可见轮廓 */
.v4-retry:focus-visible,
.chip:focus-visible,
.load-more:focus-visible {
  outline: 2px solid var(--red);
  outline-offset: 2px;
}

@media (prefers-reduced-motion: reduce) {
  .chip, .load-more { transition: none; }
  /* R30：按压缩放也关掉 */
  .chip:active, .load-more:active { transform: none; }
}
</style>
