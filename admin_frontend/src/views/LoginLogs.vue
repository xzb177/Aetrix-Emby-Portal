<script setup lang="ts">
/**
 * 登录 / 安全日志（v2.6.0）
 *
 * 记录门户与客户端登录、登录失败、设备超限被拒、诱饵码触发封禁等事件，
 * 供风控审查；支持按保留天数清理，避免表无限增长。
 */
import { computed, onMounted, ref } from 'vue'
import { RefreshCw, Trash2, ShieldAlert, ScrollText, KeyRound, Database } from 'lucide-vue-next'
import { fetchLoginLogs } from '@/api/admin'
import { PageHeader, SectionCard, StatTile } from '@/components/ui'
import { useDangerOps } from '@/composables/useDangerOps'
import type { LoginLogRow, LoginLogsResponse } from '@/types'
import DataTable from '@/components/DataTable.vue'
import type { DataColumn } from '@/components/DataTable.vue'
import FilterBar, { defaultChipText } from '@/components/FilterBar.vue'
import type { AppliedFilter, FilterConfig, FilterOption } from '@/components/FilterBar.vue'

/** 手机卡片：用户为标题，事件/结果/风险/IP/详情/时间做键值行，客户端 UA 太长故隐藏 */
const columns: DataColumn[] = [
  { key: 'username', label: '用户', width: 140, mobile: 'title' },
  { key: 'created_at', label: '时间', width: 170 },
  { key: 'reason_label', label: '事件', width: 140 },
  { key: 'success', label: '结果', width: 96 },
  { key: 'risk', label: '风险', width: 90 },
  // IP 与归属地合并成一列：配置了「IP 与地理位置」能力后，风控审查不用再去查 IP 库
  { key: 'ip', label: 'IP / 归属地', width: 190 },
  { key: 'detail', label: '详情', minWidth: 200 },
  { key: 'user_agent', label: '客户端', minWidth: 180, mobile: 'hide' },
]

type LoginLogFilters = {
  username: string
  ip: string
  reason: string
  success: string
}

type FilterKey = keyof LoginLogFilters

const data = ref<LoginLogsResponse | null>(null)
const loading = ref(false)
const loadError = ref('')

/** 草稿与提交快照分开：输入尚未提交时，刷新、chips 和空态仍对应已提交条件。 */
const draft = ref<LoginLogFilters>({
  username: '',
  ip: '',
  reason: '',
  success: '',
})
const committed = ref<LoginLogFilters>({ ...draft.value })

const reasonOptions = computed<FilterOption[]>(() => [
  { label: '全部事件', value: '' },
  ...(data.value?.reasons ?? []).map((reason) => ({
    label: reason.label,
    value: reason.value,
  })),
])

const successOptions: FilterOption[] = [
  { label: '全部结果', value: '' },
  { label: '成功', value: 'true' },
  { label: '失败', value: 'false' },
]

/** 同页有两个文本筛选器，不能共用一个 search；custom 插槽由页面提供输入框，FilterBar 负责布局。 */
const filterConfigs = computed<FilterConfig[]>(() => [
  {
    key: 'username',
    label: '用户名',
    type: 'custom',
    value: draft.value.username,
  },
  {
    key: 'ip',
    label: 'IP',
    type: 'custom',
    value: draft.value.ip,
  },
  {
    key: 'reason',
    label: '事件',
    type: 'select',
    value: draft.value.reason,
    placeholder: '全部事件',
    options: reasonOptions.value,
  },
  {
    key: 'success',
    label: '结果',
    type: 'select',
    value: draft.value.success,
    placeholder: '全部结果',
    options: successOptions,
  },
])

/** chips 只从提交快照派生，选项标签更新也只重算展示，不触发请求。 */
const appliedFilters = computed<AppliedFilter[]>(() => {
  const filters: AppliedFilter[] = []
  const value = committed.value

  if (value.username) {
    filters.push({ key: 'username', label: '用户名', text: value.username })
  }
  if (value.ip) {
    filters.push({ key: 'ip', label: 'IP', text: value.ip })
  }
  if (value.reason) {
    filters.push({
      key: 'reason',
      label: '事件',
      text: defaultChipText(value.reason, reasonOptions.value),
    })
  }
  if (value.success) {
    filters.push({
      key: 'success',
      label: '结果',
      text: value.success === 'true' ? '成功' : '失败',
    })
  }

  return filters
})

const committedHasFilter = computed(() =>
  Boolean(
    committed.value.username ||
      committed.value.ip ||
      committed.value.reason ||
      committed.value.success,
  ),
)

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    data.value = await fetchLoginLogs({
      username: committed.value.username || undefined,
      ip: committed.value.ip || undefined,
      reason: committed.value.reason || undefined,
      success: committed.value.success === '' ? undefined : committed.value.success === 'true',
      limit: 300,
    })
  } catch (e) {
    // 查询失败拦截器不弹提示，由表格错误态兜底（保留上一次的数据与统计）
    loadError.value = e instanceof Error ? e.message : '加载失败'
  } finally {
    loading.value = false
  }
}

/** 先标准化再比对快照：重复回车、清空或查询不重复请求，也不为筛选引入额外等待。 */
function commitQuery() {
  const next: LoginLogFilters = {
    username: draft.value.username.trim(),
    ip: draft.value.ip.trim(),
    reason: draft.value.reason,
    success: draft.value.success,
  }
  const previous = committed.value

  if (
    next.username === previous.username &&
    next.ip === previous.ip &&
    next.reason === previous.reason &&
    next.success === previous.success
  ) {
    return
  }

  committed.value = next
  void load()
}

function isFilterKey(key: string): key is FilterKey {
  return key === 'username' || key === 'ip' || key === 'reason' || key === 'success'
}

function handleFilterChange(key: string, value: unknown) {
  if (!isFilterKey(key) || typeof value !== 'string') return
  draft.value[key] = value
  commitQuery()
}

function removeFilter(key: string) {
  if (!isFilterKey(key)) return
  draft.value[key] = ''
  commitQuery()
}

function clearFilters() {
  draft.value = { username: '', ip: '', reason: '', success: '' }
  commitQuery()
}

onMounted(load)

/** 清理动作进行中（按钮 loading，防重复点击） */
const purgeBusy = ref(false)
/** 危险操作共用实现（与「系统设置 → 危险操作」页签同一份） */
const dangerOps = useDangerOps()

/** 清理日志是危险操作：确认文案与执行都在 useDangerOps（与「系统设置 → 危险操作」同一份） */
async function purge(preset: number) {
  purgeBusy.value = true
  try {
    if (await dangerOps.purgeLogs(preset)) load()
  } finally {
    purgeBusy.value = false
  }
}

function fmt(s: string | null): string {
  if (!s) return '—'
  return s.slice(0, 19).replace('T', ' ')
}

function riskBadge(row: LoginLogRow): string {
  const level = riskLevel(row)
  return level === '高风险' ? 'au-badge-rose' : level === '注意' ? 'au-badge-amber' : 'au-badge-green'
}

function riskLevel(row: LoginLogRow): string {
  if (row.reason === 'decoy_code' || row.reason === 'device_limit') return '高风险'
  if (!row.success) return '注意'
  return '正常'
}
</script>

<template>
  <div class="admin-page">
    <PageHeader
      eyebrow="用户"
      title="登录日志"
      description="登录成功 / 失败、设备超限、诱饵码触发等风控事件；最多加载最近 300 条。"
    >
      <template #actions>
        <RouterLink class="danger-jump" :to="{ name: 'Settings', query: { tab: 'danger', op: 'logs' } }">
          危险操作中心 →
        </RouterLink>
        <el-button type="danger" :loading="purgeBusy" @click="purge(data && data.total > 0 ? 90 : 0)" :icon="Trash2">
          清理日志
        </el-button>
        <el-button :loading="loading" @click="load" :icon="RefreshCw">刷新</el-button>
      </template>
    </PageHeader>

    <div class="stat-row">
      <StatTile label="日志总数" :value="data?.total ?? 0" :icon="Database" hint="按保留天数自动清理" />
      <StatTile
        label="24h 登录失败"
        :value="data?.summary.failed_24h ?? 0"
        :icon="KeyRound"
        :tone="(data?.summary.failed_24h ?? 0) > 0 ? 'warn' : 'plain'"
        hint="含客户端与门户登录失败"
      />
      <StatTile
        label="24h 风控拦截"
        :value="data?.summary.risk_24h ?? 0"
        :icon="ShieldAlert"
        :tone="(data?.summary.risk_24h ?? 0) > 0 ? 'danger' : 'plain'"
        hint="设备超限 / 诱饵码触发"
      />
    </div>

    <SectionCard title="登录事件" :icon="ScrollText" :meta="data ? `${data.logs.length} 条` : ''" flush>
      <div class="list-bar">
        <!-- 多输入框且无主搜索的页面也需要统一查询按钮，因此只启用 queryButton，不传 search。 -->
        <FilterBar
          class="fb-grow"
          :filters="filterConfigs"
          :applied-filters="appliedFilters"
          :query-button="true"
          :loading="loading"
          @filter-change="handleFilterChange"
          @query="commitQuery"
          @remove-chip="removeFilter"
          @clear="clearFilters"
        >
          <template #filter-username>
            <el-input
              v-model="draft.username"
              placeholder="用户名"
              clearable
              @keyup.enter="commitQuery"
              @clear="removeFilter('username')"
            />
          </template>
          <template #filter-ip>
            <el-input
              v-model="draft.ip"
              placeholder="IP"
              clearable
              @keyup.enter="commitQuery"
              @clear="removeFilter('ip')"
            />
          </template>
        </FilterBar>
      </div>

      <DataTable
        :rows="data?.logs || []"
        :columns="columns"
        :loading="loading"
        :error="loadError"
        empty="暂无登录记录"
        :has-active-filters="committedHasFilter"
        @retry="load"
        @clear-filters="clearFilters"
      >
        <template #cell-username="{ row }">
          <span class="user-name">{{ row.username || '—' }}</span>
        </template>

        <template #cell-created_at="{ row }"><span class="mono">{{ fmt(row.created_at) }}</span></template>

        <template #cell-reason_label="{ row }">{{ row.reason_label }}</template>

        <template #cell-success="{ row }">
          <span class="au-badge" :class="row.success ? 'au-badge-green' : 'au-badge-rose'">
            {{ row.success ? '成功' : '失败' }}
          </span>
        </template>

        <template #cell-risk="{ row }">
          <span class="au-badge" :class="riskBadge(row)">{{ riskLevel(row) }}</span>
        </template>

        <template #cell-ip="{ row }">
          <span class="mono">{{ row.ip || '—' }}</span>
          <span v-if="row.region" class="region">{{ row.region }}</span>
        </template>

        <template #cell-detail="{ row }">
          <span v-if="!row.detail" class="muted">—</span>
          <span v-else>{{ row.detail }}</span>
        </template>

        <template #cell-user_agent="{ row }">
          <span class="ua">{{ row.user_agent || '—' }}</span>
        </template>
      </DataTable>
    </SectionCard>
  </div>
</template>

<style scoped>
.stat-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
}

.list-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px 12px;
  flex-wrap: wrap;
  padding: 4px 20px 12px;
}

.list-bar .fb-grow { flex: 1 1 auto; }
.list-bar .head-actions { justify-content: flex-end; }

/* 危险操作全部收在「系统设置 → 危险操作」：这里只留一个入口，不开第二个现场 */
.danger-jump {
  color: var(--au-text-3);
  text-decoration: none;
  font-size: 12px;
  padding: 0 4px;
}
.danger-jump:hover { color: var(--au-danger); text-decoration: underline; }
.danger-jump:focus-visible { outline: 2px solid var(--au-border-focus); outline-offset: 2px; border-radius: var(--au-r-sm); }

.user-name { font-weight: 600; color: var(--au-text); }
.muted { color: var(--au-text-4); }

.region {
  display: block;
  font-size: 12px;
  color: var(--au-text-3);
}

.ua {
  font-size: 12px;
  color: var(--au-text-3);
  display: inline-block;
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

@media (max-width: 768px) {
  .list-bar { padding: 4px 16px 12px; }
}

@media (max-width: 640px) {
  .stat-row { grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; }
  .ua { max-width: 100%; }
}
</style>
