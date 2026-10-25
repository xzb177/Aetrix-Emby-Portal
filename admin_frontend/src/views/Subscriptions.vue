<script setup lang="ts">
/**
 * 会员管理
 *
 * v2.4.0 新增：此前会员只能靠数据库查看——用户管理页只显示「生效中/无会员」两态。
 * 本页提供会员总览：生效中 / 即将到期 / 已过期，支持搜索、状态筛选、延长与授予。
 *
 * v2.6.20：会员是**一个服一个**的。默认只看面板当前服（会员开在哪个服、能在哪台 EA 上播
 * 都由它决定），顶部可以切到「全部服」做跨服汇总——这时列表会多一列归属服。
 *
 * v2.54（暗房影院）：PageHeader（范围切换 + 刷新）· 四个 StatTile 兼作状态筛选按钮
 * （外面包一层 button，选中走琥珀描边）· 到期提醒与订阅清单都换成 SectionCard，
 * 清单是 flush 表格 + 统一工具条；加载失败给可重试的错误态。
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import {
  AlertTriangle, BellRing, CalendarClock, Crown, ListChecks, RefreshCw, Search, TimerOff, Users,
} from 'lucide-vue-next'
import { EmptyState, PageHeader, SectionCard, StatTile } from '@/components/ui'
import { fetchPlans, fetchRealmSubscriptions, type PlanRow } from '@/api/admin'
import {
  extendUserSubscription, grantUserSubscription,
  fetchReminderStatus, runReminders, updateReminderSettings,
  type ReminderStatus,
} from '@/api/economy'
import type { SubscriptionOverviewRow } from '@/types'
import { useQueryFilter } from '@/composables/useQueryFilter'
import { useRealmStore } from '@/stores/realm'
import DataTable from '@/components/DataTable.vue'
import type { DataColumn } from '@/components/DataTable.vue'

const realm = useRealmStore()
/** 统计范围：当前服（默认）或全部服 */
const scope = ref<'realm' | 'all'>('realm')
const scopeRealmName = ref('')

/** 会员列表：手机端用户名做标题，「剩余」保留桌面端排序 */
const columns = computed<DataColumn[]>(() => [
  { key: 'username', label: '用户', minWidth: 140, mobile: 'title' },
  { key: 'plan_name', label: '套餐', minWidth: 140 },
  // 只有跨服汇总时才需要归属服这一列
  ...(scope.value === 'all'
    ? [{ key: 'realm_name', label: '归属服', minWidth: 130 } as DataColumn]
    : []),
  { key: 'period', label: '有效期', minWidth: 200 },
  { key: 'days_left', label: '剩余', width: 110, sortable: true },
  { key: 'status', label: '状态', width: 110 },
  { key: 'actions', label: '操作', width: 170, fixed: 'right', align: 'right' },
])

const loading = ref(false)
const rows = ref<SubscriptionOverviewRow[]>([])
const summary = ref({ total: 0, active: 0, expiring_7d: 0, expired: 0 })
const statusFilter = ref<string>('')
const search = ref('')
/** 清单加载失败（区别于「没有符合条件的记录」） */
const loadError = ref(false)
// 深链：仪表盘 / 命令面板带筛选过来（Phase 5，如 /subscriptions?status=expiring）
useQueryFilter(statusFilter, 'status', load)

/** 顶部四个数字块 = 四个筛选入口（点一下就把下面的清单筛成这个状态） */
function filterBy(status: string) {
  statusFilter.value = statusFilter.value === status ? '' : status
  load()
}
const plans = ref<PlanRow[]>([])

/** 请求序号：连续切筛选 / 范围时只采纳最后一次请求，避免旧响应覆盖新结果 */
let loadSeq = 0
/** 最近一次实际查询用的关键字：防抖回调据此跳过重复查询 */
let lastSearch = ''

async function load() {
  const seq = ++loadSeq
  clearTimeout(searchTimer)
  lastSearch = search.value.trim()
  loading.value = true
  loadError.value = false
  try {
    const params: Record<string, unknown> = { limit: 200 }
    if (statusFilter.value) params.status_filter = statusFilter.value
    if (search.value.trim()) params.search = search.value.trim()
    // realm_id=0 → 全部服；其余按服过滤（后端 `active_realm_id` 作为兜底）
    const res = await fetchRealmSubscriptions(scope.value === 'all' ? 0 : realm.activeId ?? 0, params)
    if (seq !== loadSeq) return
    rows.value = res.subscriptions
    summary.value = res.summary
    scopeRealmName.value = res.realm_name
  } catch {
    if (seq !== loadSeq) return
    // 错误提示由 HTTP 拦截器统一处理；这里只记下「失败了」好给出重试入口
    loadError.value = true
  } finally {
    if (seq === loadSeq) loading.value = false
  }
}

/** 搜索框输入防抖：停手 350ms 自动查询（回车 / 清空仍立即查询） */
let searchTimer: ReturnType<typeof setTimeout> | undefined
watch(search, (value) => {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    if (value.trim() !== lastSearch) load()
  }, 350)
})
onBeforeUnmount(() => clearTimeout(searchTimer))

/** 可授予的套餐：跨服汇总时列出全部服的套餐（授予时按套餐所属的服开会员） */
async function loadPlans() {
  try {
    plans.value = (await fetchPlans(scope.value === 'all' ? 0 : undefined)).plans
  } catch {
    plans.value = []
  }
}

// 三个接口互不依赖：并行拉，别排成瀑布（原来要等清单回来才开始拉套餐与提醒状态）
onMounted(() => {
  load()
  loadPlans()
  loadReminders()
})

function onScopeChange() {
  load()
  loadPlans()
}

const activeCount = computed(() => summary.value.active)

const hasFilter = computed(() => !!(statusFilter.value || search.value.trim()))

function resetFilters() {
  statusFilter.value = ''
  search.value = ''
  load()
}

/** 顶部数字块（兼作筛选按钮）：数字走正文色，只有「7 天内到期 > 0」染警示 */
const statTiles = computed(() => [
  { key: 'active', label: '生效中', value: summary.value.active, icon: Crown, tone: 'plain' as const, title: '只看生效中的会员' },
  {
    key: 'expiring', label: '7 天内到期', value: summary.value.expiring_7d, icon: CalendarClock,
    tone: summary.value.expiring_7d > 0 ? ('warn' as const) : ('plain' as const), title: '只看 7 天内到期的会员',
  },
  { key: 'expired', label: '已过期', value: summary.value.expired, icon: TimerOff, tone: 'plain' as const, title: '只看已过期的会员' },
  { key: '', label: '记录总数', value: summary.value.total, icon: Users, tone: 'plain' as const, title: '取消筛选，看全部记录' },
])

function daysTone(row: SubscriptionOverviewRow): string {
  if (row.days_left <= 0) return 'off'
  if (row.days_left <= 7) return 'warn'
  return 'ok'
}

/** 状态徽章：生效中绿 / 临期警示 / 已过期中性 */
function badgeClass(row: SubscriptionOverviewRow): string {
  const tone = daysTone(row)
  if (tone === 'ok') return 'au-badge-green'
  if (tone === 'warn') return 'badge-warn'
  return 'au-badge-muted'
}

function statusText(row: SubscriptionOverviewRow): string {
  if (row.days_left <= 0) return '已过期'
  if (row.days_left <= 7) return '即将到期'
  return '生效中'
}

function fmtDay(s: string | null): string {
  return s ? s.slice(0, 10) : '—'
}

// ==================== 到期续费提醒（v2.8.0） ====================
// 会员到期前按 7/3/1 天自动发站内信（1 小时检查一次，靠去重表保证不重复打扰）。
// 这里给出「到底有没有在跑」的口径：档位、待发条数、最近发送记录，并可手动跑一轮。
const reminders = ref<ReminderStatus | null>(null)
const reminderSaving = ref(false)
const reminderRunning = ref(false)
const reminderDays = ref('')

async function loadReminders() {
  try {
    reminders.value = await fetchReminderStatus()
    reminderDays.value = reminders.value.thresholds.join(',')
  } catch {
    // 错误提示由 HTTP 拦截器统一处理
  }
}

async function saveReminderDays() {
  reminderSaving.value = true
  try {
    const res = await updateReminderSettings({ expiry_reminder_days: reminderDays.value })
    reminders.value = res.status
    reminderDays.value = res.status.thresholds.join(',')
    ElMessage.success(`提醒档位已保存：${res.status.thresholds.map((d) => `${d} 天`).join(' / ')}`)
  } catch {
    await loadReminders()
  } finally {
    reminderSaving.value = false
  }
}

/** dryRun=true 只看会发给谁，不打扰任何用户 */
async function runReminderPass(dryRun: boolean) {
  reminderRunning.value = true
  try {
    const res = await runReminders(dryRun)
    if (!res.enabled) {
      ElMessage.warning('到期提醒未开启（由服务端配置控制）')
    } else if (dryRun) {
      ElMessage.info(res.due.length ? `待发 ${res.due.length} 条（未发送）` : '当前没有待发的提醒')
    } else if (res.reminded + res.expired_notified === 0) {
      ElMessage.info(res.skipped ? `无需重复提醒（跳过 ${res.skipped} 条）` : '当前没有待发的提醒')
    } else {
      ElMessage.success(`已发送：临期 ${res.reminded} 条 / 已到期 ${res.expired_notified} 条`)
    }
    await loadReminders()
  } catch {
    // 拦截器已提示
  } finally {
    reminderRunning.value = false
  }
}

/** 发送档位的中文说明（面板上不想让运营自己去猜 kind 的含义） */
function kindText(kind: string): string {
  return kind === 'expired' ? '已到期' : `剩 ${kind.replace('d', '')} 天`
}

// ==================== 延长 / 授予 ====================

const dialog = reactive({
  visible: false,
  mode: 'extend' as 'extend' | 'grant',
  row: null as SubscriptionOverviewRow | null,
  planId: undefined as number | undefined,
  days: 30,
})
const saving = ref(false)

function openExtend(row: SubscriptionOverviewRow) {
  dialog.mode = 'extend'
  dialog.row = row
  dialog.days = row.days_left > 0 ? 30 : 30
  dialog.visible = true
}

function openGrant(row: SubscriptionOverviewRow) {
  dialog.mode = 'grant'
  dialog.row = row
  dialog.planId = plans.value[0]?.id
  dialog.days = plans.value[0]?.duration_days ?? 30
  dialog.visible = true
}

function onPlanChange(id: number | undefined) {
  const plan = plans.value.find((p) => p.id === id)
  if (plan) dialog.days = plan.duration_days
}

async function submit() {
  const row = dialog.row
  if (!row) return
  saving.value = true
  try {
    if (dialog.mode === 'extend') {
      await extendUserSubscription(row.id, dialog.days)
      ElMessage.success('会员已延长并通知用户')
    } else {
      if (!dialog.planId) {
        ElMessage.warning('请选择套餐')
        return
      }
      const plan = plans.value.find((p) => p.id === dialog.planId)
      await grantUserSubscription(row.user_id, {
        plan_id: dialog.planId,
        duration_days: dialog.days,
        // 会员开在哪个服：优先跟随所选套餐，避免在多服面板里开错服
        ...(plan?.realm_id ? { realm_id: plan.realm_id } : {}),
      })
      ElMessage.success('会员已授予并通知用户')
    }
    dialog.visible = false
    await load()
  } catch {
    // 拦截器已提示
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="admin-page subs-page">
    <PageHeader eyebrow="用户" title="会员与权益">
      <template #description>
        共 {{ summary.total }} 条订阅记录 · {{ activeCount }} 位会员生效中
        <template v-if="scopeRealmName">· 范围：{{ scopeRealmName }}</template>
      </template>
      <template #actions>
        <el-radio-group v-model="scope" @change="onScopeChange">
          <el-radio-button value="realm">当前服</el-radio-button>
          <el-radio-button value="all">全部服</el-radio-button>
        </el-radio-group>
        <el-button :icon="RefreshCw" :loading="loading" @click="load">刷新</el-button>
      </template>
    </PageHeader>

    <!-- 四个数字块 = 四个筛选入口：点一下把下面的清单筛成这个状态，再点一下取消 -->
    <section class="stat-row" aria-label="会员概况（点击筛选）">
      <button
        v-for="t in statTiles"
        :key="t.key || 'all'"
        type="button"
        class="tile-btn"
        :class="{ 'is-active': statusFilter === t.key }"
        :aria-pressed="statusFilter === t.key"
        :title="t.title"
        @click="filterBy(t.key)"
      >
        <StatTile :label="t.label" :value="t.value" :icon="t.icon" :tone="t.tone" />
      </button>
    </section>

    <!-- 到期提醒：会员到期前自动触达，是续费率最直接的一环；面板要能看出它在跑 -->
    <SectionCard :icon="BellRing" description="会员到期前按档位自动发站内信；每个档位只提醒一次。">
      <template #title>
        到期续费提醒
        <span class="au-badge" :class="reminders?.enabled ? 'au-badge-green' : 'au-badge-muted'">
          {{ reminders?.enabled ? '已开启' : '已关闭' }}
        </span>
      </template>
      <template #actions>
        <el-button size="small" :loading="reminderRunning" @click="runReminderPass(true)">预览</el-button>
        <el-button
          size="small"
          type="primary"
          :loading="reminderRunning"
          :disabled="!reminders?.enabled"
          @click="runReminderPass(false)"
        >立即检查并发送</el-button>
      </template>

      <div class="reminder-body">
        <div class="reminder-field">
          <label class="field-label" for="reminder-days">提前提醒档位</label>
          <div class="reminder-input">
            <el-input id="reminder-days" v-model="reminderDays" size="small" placeholder="7,3,1" class="days-input" />
            <el-button size="small" :loading="reminderSaving" @click="saveReminderDays">保存</el-button>
          </div>
          <p class="field-hint">
            逗号分隔的天数（当前：{{ reminders?.thresholds?.join(' / ') || '—' }} 天）
          </p>
        </div>
        <dl class="reminder-stats">
          <div class="reminder-stat">
            <dt>待发 · 临期</dt>
            <dd>{{ reminders?.pending_reminders ?? '—' }}</dd>
          </div>
          <div class="reminder-stat">
            <dt>待发 · 已到期</dt>
            <dd>{{ reminders?.pending_expired ?? '—' }}</dd>
          </div>
          <div class="reminder-stat">
            <dt>累计已发</dt>
            <dd>{{ reminders?.total_sent ?? '—' }}</dd>
          </div>
          <div class="reminder-stat">
            <dt>检查间隔</dt>
            <dd>{{ reminders ? Math.round(reminders.interval_seconds / 60) + ' 分钟' : '—' }}</dd>
          </div>
        </dl>
      </div>

      <template #footer>
        <div v-if="reminders?.recent?.length" class="reminder-recent">
          <span class="rr-label">最近发送</span>
          <span v-for="r in reminders.recent.slice(0, 6)" :key="r.id" class="au-badge au-badge-muted">
            {{ r.username }}<em class="rr-kind">{{ kindText(r.kind) }}</em>
          </span>
        </div>
        <template v-else>还没有发送记录。已有会员临期时，这里会出现「谁 · 哪一档」的明细。</template>
      </template>
    </SectionCard>

    <!-- 订阅清单：筛选在左、清空在右；桌面表格 / 手机卡片 -->
    <SectionCard title="订阅清单" :icon="ListChecks" :meta="rows.length ? `${rows.length} 条` : ''" flush>
      <div class="view-toolbar">
        <div class="view-toolbar__filters">
          <el-input
            v-model="search"
            class="f-search"
            placeholder="搜索用户名"
            clearable
            @keyup.enter="load"
            @clear="load"
          >
            <template #prefix><Search :size="14" /></template>
          </el-input>
          <el-select v-model="statusFilter" class="f-select" placeholder="全部状态" clearable @change="load">
            <el-option label="生效中" value="active" />
            <el-option label="7 天内到期" value="expiring" />
            <el-option label="已过期" value="expired" />
          </el-select>
        </div>
        <div class="view-toolbar__actions">
          <el-button v-if="hasFilter" text @click="resetFilters">清空筛选</el-button>
          <el-button type="primary" :icon="Search" @click="load">查询</el-button>
        </div>
      </div>

      <EmptyState
        v-if="loadError && !rows.length"
        :icon="AlertTriangle"
        title="订阅清单加载失败"
        description="网络或服务暂时不可用，稍后重试。"
      >
        <template #actions><el-button :loading="loading" @click="load">重试</el-button></template>
      </EmptyState>

      <DataTable v-else class="flush-table" :rows="rows" :columns="columns" :loading="loading" empty="没有符合条件的订阅记录">
        <template #cell-username="{ row }">
          <span class="user-name">{{ row.username }}</span>
        </template>

        <template #cell-plan_name="{ row }">{{ row.plan_name }}</template>

        <template #cell-realm_name="{ row }">
          <span class="au-badge au-badge-muted">{{ row.realm_name || '未标注' }}</span>
        </template>

        <template #cell-period="{ row }">
          <span class="period">{{ fmtDay(row.start_date) }} → {{ fmtDay(row.end_date) }}</span>
        </template>

        <template #cell-days_left="{ row }">
          <span class="days" :class="`is-${daysTone(row)}`">{{ row.days_left }} 天</span>
        </template>

        <template #cell-status="{ row }">
          <span class="au-badge" :class="badgeClass(row)">{{ statusText(row) }}</span>
        </template>

        <template #cell-actions="{ row }">
          <el-button
            v-if="row.days_left > 0"
            size="small"
            text
            type="primary"
            @click="openExtend(row)"
          >延长</el-button>
          <el-button v-else size="small" text type="primary" @click="openGrant(row)">续订</el-button>
        </template>

        <template #empty>
          <EmptyState
            compact
            :icon="Crown"
            :title="hasFilter ? '没有符合条件的订阅记录' : '还没有订阅记录'"
            :description="hasFilter ? '换个条件，或清空筛选看全部。' : '用户购买套餐或被授予订阅后会出现在这里。'"
          >
            <template v-if="hasFilter" #actions>
              <el-button size="small" @click="resetFilters">清空筛选</el-button>
            </template>
          </EmptyState>
        </template>
      </DataTable>
    </SectionCard>

    <el-dialog
      v-model="dialog.visible"
      :title="dialog.mode === 'extend' ? '延长会员' : '续订 / 授予会员'"
      width="min(440px, 92vw)"
    >
      <el-form label-position="top">
        <el-form-item label="用户">
          <span class="dialog-user">{{ dialog.row?.username }}</span>
        </el-form-item>
        <el-form-item v-if="dialog.mode === 'extend'" label="当前到期">
          <span class="dialog-user">
            {{ fmtDay(dialog.row?.end_date ?? null) }}
            <em class="muted">（剩余 {{ dialog.row?.days_left }} 天）</em>
          </span>
        </el-form-item>
        <el-form-item v-else label="套餐">
          <el-select v-model="dialog.planId" style="width: 100%" @change="onPlanChange">
            <el-option
              v-for="p in plans"
              :key="p.id"
              :label="`${p.name}（${p.duration_days} 天 / ¥${p.price}）`"
              :value="p.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="天数">
          <el-input-number v-model="dialog.days" :min="1" :max="3650" style="width: 100%" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog.visible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submit">确认</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
/* ---------- 数字块兼作筛选按钮：外层 button 只负责交互，外观交给 StatTile ---------- */
.stat-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 12px;
}

.tile-btn {
  display: block;
  padding: 0;
  border: 0;
  background: none;
  font: inherit;
  color: inherit;
  text-align: left;
  cursor: pointer;
  border-radius: var(--au-r-lg);
}

.tile-btn :deep(.au-stat) { height: 100%; transition: border-color var(--au-fast) var(--au-ease), background var(--au-fast) var(--au-ease); }
.tile-btn:hover :deep(.au-stat) { border-color: var(--au-border-strong); background: var(--au-surface-2); }
.tile-btn.is-active :deep(.au-stat) { border-color: var(--au-primary-border); background: var(--au-primary-soft); }
.tile-btn:focus-visible { outline: 2px solid var(--au-border-focus); outline-offset: 2px; }

/* ---------- 工具条：筛选在左、动作在右 ---------- */
.view-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px 12px;
  flex-wrap: wrap;
  padding: 4px 20px 14px;
  border-bottom: 1px solid var(--au-border);
}

.view-toolbar__filters { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; flex: 1 1 auto; min-width: 0; }
.view-toolbar__actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.f-search { width: 220px; }
.f-select { width: 140px; }

.flush-table :deep(.dt-cards) { padding: 12px 12px 8px; }

/* ---------- 表格单元 ---------- */
.period { font-variant-numeric: tabular-nums; color: var(--au-text-2); }
.days { font-weight: 600; font-variant-numeric: tabular-nums; }
.days.is-ok { color: var(--au-text); }
.days.is-warn { color: var(--au-warning); }
.days.is-off { color: var(--au-text-4); }
.badge-warn { background: var(--au-warning-soft); color: var(--au-warning); border-color: var(--au-warning-border); }

/* ---------- 到期提醒 ---------- */
.reminder-body {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px 24px;
  flex-wrap: wrap;
}

.field-label { display: block; margin-bottom: 6px; font-size: 12px; color: var(--au-text-3); }
.reminder-input { display: flex; align-items: center; gap: 8px; }
.days-input { width: 140px; }
.field-hint { margin: 6px 0 0; font-size: 12px; color: var(--au-text-3); }

.reminder-stats { display: flex; gap: 8px 24px; flex-wrap: wrap; margin: 0; }
.reminder-stat { display: flex; flex-direction: column; gap: 2px; }
.reminder-stat dt { font-size: 12px; color: var(--au-text-3); }
.reminder-stat dd { margin: 0; font-size: 16px; font-weight: 700; font-variant-numeric: tabular-nums; color: var(--au-text); }

.reminder-recent { display: flex; align-items: center; gap: 6px 8px; flex-wrap: wrap; }
.rr-label { color: var(--au-text-3); }
.rr-kind { font-style: normal; color: var(--au-text-4); margin-left: 4px; }

.dialog-user { font-weight: 600; }
.dialog-user em { font-style: normal; font-size: 12px; color: var(--au-text-3); font-weight: 400; }

@media (max-width: 768px) {
  .stat-row { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
  .view-toolbar { padding: 2px 16px 12px; }
  .view-toolbar__filters > .f-search { flex: 1 1 100%; width: auto; }
  .view-toolbar__filters > .f-select { flex: 1 1 120px; width: auto; }
  .view-toolbar__actions { width: 100%; justify-content: flex-end; }
  .reminder-stats { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); width: 100%; }
  .reminder-field, .reminder-input { width: 100%; }
  .days-input { flex: 1; width: auto; }
}
</style>
