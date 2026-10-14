<script setup lang="ts">
/**
 * 旧版兑换码管理（Batch F 码体系正名）
 *
 * 老系统遗留的码：只查旧码，不再新发（新发放统一走「会员卡码」）。
 * 功能：停用 / 启用、使用审计。
 * 「兑换码」名称保留（抵钱的码叫兑换码是拍板口径），但页内一律标「旧版」以和会员卡码区分。
 */
import { onMounted, onUnmounted, ref, computed, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Gift, RefreshCw, CheckCircle2, Ticket } from 'lucide-vue-next'
import { PageHeader, SectionCard, StatTile } from '@/components/ui'
import DataTable from '@/components/DataTable.vue'
import type { DataColumn } from '@/components/DataTable.vue'
import FilterBar from '@/components/FilterBar.vue'
import type { FilterConfig, AppliedFilter } from '@/components/FilterBar.vue'
import { useTableSelection } from '@/composables/useTableSelection'
import { runBulkOperation, bulkSummaryText, BULK_LIMIT } from '@/utils/runBulkOperation'
import type { RowKey } from '@/composables/useTableSelection'

/** 手机卡片：兑换码为标题，奖励/使用/状态/有效期做键值行 */
const columns: DataColumn[] = [
  { key: 'code', label: '兑换码', width: 180, mobile: 'title' },
  { key: 'type', label: '类型', width: 90 },
  { key: 'reward', label: '奖励内容', minWidth: 150 },
  { key: 'use_count', label: '使用', width: 90 },
  { key: 'expires_at', label: '有效期至', width: 120 },
  { key: 'note', label: '备注', minWidth: 120, mobile: 'hide' },
  { key: 'is_active', label: '状态', width: 90 },
  { key: 'used_by', label: '核销记录', minWidth: 160, mobile: 'hide' },
  { key: 'actions', label: '操作', width: 110, fixed: 'right', align: 'right' },
]
import {
  fetchExchangeCodes,
  updateExchangeCode,
  type ExchangeCodeRow,
} from '@/api/economy'

const loading = ref(false)
const loadError = ref('')
const codes = ref<ExchangeCodeRow[]>([])

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const c = await fetchExchangeCodes({ limit: 200 })
    codes.value = c.codes
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : '加载失败'
  } finally {
    loading.value = false
  }
}

const toggleBusyId = ref<number | null>(null)
async function toggleCode(row: ExchangeCodeRow) {
  if (toggleBusyId.value === row.id) return
  toggleBusyId.value = row.id
  try {
    await updateExchangeCode(row.id, !row.is_active)
    row.is_active = !row.is_active
    ElMessage.success(row.is_active ? '已启用' : '已停用')
  } catch {
    /* 写操作失败由请求拦截器统一弹错 */
  } finally {
    toggleBusyId.value = null
  }
}

/**
 * 兑换码批量停用（v2.58）
 * - DataTable 绑的是前端筛选后的 visibleCodes，选择基于它
 * - 兑换码没有删除接口，只有停用：循环调 updateExchangeCode(id, false)，并发 2
 */
const selection = useTableSelection()
/** 模板 v-model 用的可写代理：嵌套 ref 在模板里不会自动解包 */
const selectedKeys = computed<RowKey[]>({
  get: () => selection.selectedKeys.value,
  set: (v) => { selection.selectedKeys.value = v },
})
const bulkBusy = ref(false)

async function bulkDeactivate() {
  const ids = selection.selectedKeys.value as number[]
  if (!ids.length) return
  if (ids.length > BULK_LIMIT) {
    ElMessage.warning(`一次最多操作 ${BULK_LIMIT} 项`)
    return
  }
  try {
    await ElMessageBox.confirm(
      `确定要停用选中的 ${ids.length} 个兑换码吗？停用后这些码不能再被使用。`,
      '批量停用',
      { type: 'warning', confirmButtonText: '确认停用', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  bulkBusy.value = true
  try {
    const summary = await runBulkOperation(ids, (id) => updateExchangeCode(id, false), { concurrency: 2 })
    // 失败项保留选中态可重试
    selection.selectedKeys.value = summary.failedKeys as RowKey[]
    ElMessage[summary.failed ? 'warning' : 'success'](bulkSummaryText(summary, '停用'))
    // 列表是前端筛选（visibleCodes），直接更新本地状态即可，无需重拉（重拉会重置滚动）
    for (const id of ids) {
      if (!summary.failedKeys.includes(id)) {
        const row = codes.value.find((c) => c.id === id)
        if (row) row.is_active = false
      }
    }
  } finally {
    bulkBusy.value = false
  }
}

const rewardText = (row: ExchangeCodeRow) =>
  row.type === 'points'
    ? `${row.points_value} 积分`
    : row.type === 'subscription'
      ? `${row.plan_name || '套餐'} × ${row.duration_days} 天`
      : `${row.discount_pct / 10} 折`

function usedNames(row: ExchangeCodeRow): string {
  return (row.used_by || []).map(u => u.username).join('、')
}

const usedSummary = computed(() =>
  codes.value.reduce((acc, c) => acc + c.use_count, 0),
)

const activeCount = computed(() => codes.value.filter((c) => c.is_active).length)

// 前端筛选（后端一次给最近 200 条）
/** FilterBar 用 debounceMs: 0 逐键同步草稿；250ms 防抖只由下面的 watch 负责，避免叠加等待 */
const keyword = ref('')
/** 实际参与筛选的关键字：输入停 250ms 再过滤，连续打字时不逐键重算整张表 */
const appliedKeyword = ref('')
let keywordTimer: ReturnType<typeof setTimeout> | undefined
watch(keyword, (kw) => {
  clearTimeout(keywordTimer)
  // 清空是立即的：点 × 或「清空筛选」不用等；筛选关键字变化时清掉旧选择
  if (!kw.trim()) {
    appliedKeyword.value = ''
    selection.clear()
  } else {
    keywordTimer = setTimeout(() => {
      appliedKeyword.value = kw
      selection.clear()
    }, 250)
  }
})
onUnmounted(() => clearTimeout(keywordTimer))
const typeFilter = ref<'' | 'points' | 'subscription' | 'discount'>('')
const statusFilter = ref<'' | 'active' | 'inactive'>('')
const hasFilter = computed(() => Boolean(keyword.value.trim() || typeFilter.value || statusFilter.value))

const filters = computed<FilterConfig[]>(() => [
  {
    key: 'type',
    label: '类型',
    type: 'select',
    value: typeFilter.value,
    placeholder: '全部类型',
    options: [
      { label: '全部类型', value: '' },
      { label: '积分', value: 'points' },
      { label: '订阅', value: 'subscription' },
      { label: '折扣', value: 'discount' },
    ],
  },
  {
    key: 'status',
    label: '状态',
    type: 'select',
    value: statusFilter.value,
    placeholder: '全部状态',
    options: [
      { label: '全部状态', value: '' },
      { label: '启用', value: 'active' },
      { label: '停用', value: 'inactive' },
    ],
  },
])

function typeLabel(type: typeof typeFilter.value): string {
  return type === 'points' ? '积分' : type === 'subscription' ? '订阅' : type === 'discount' ? '折扣' : ''
}

/** chips 只读已应用值；输入防抖期间不提前展示草稿，也不触发任何请求 */
const appliedFilters = computed<AppliedFilter[]>(() => {
  const applied: AppliedFilter[] = []
  if (appliedKeyword.value) {
    applied.push({ key: 'keyword', label: '关键词', text: appliedKeyword.value })
  }
  if (typeFilter.value) {
    applied.push({ key: 'type', label: '类型', text: typeLabel(typeFilter.value) })
  }
  if (statusFilter.value) {
    applied.push({
      key: 'status',
      label: '状态',
      text: statusFilter.value === 'active' ? '启用' : '停用',
    })
  }
  return applied
})

const visibleCodes = computed(() => {
  const kw = appliedKeyword.value.trim().toLowerCase()
  return codes.value.filter((c) => {
    if (typeFilter.value && c.type !== typeFilter.value) return false
    if (statusFilter.value === 'active' && !c.is_active) return false
    if (statusFilter.value === 'inactive' && c.is_active) return false
    if (!kw) return true
    return [c.code, c.note, c.plan_name, usedNames(c)].join(' ').toLowerCase().includes(kw)
  })
})

/** select 的 ref 就是上次提交快照；同值直接返回，变化才清选择并立即筛选，全程零请求 */
function handleFilterChange(key: string, value: unknown) {
  if (key === 'type') {
    if (value !== '' && value !== 'points' && value !== 'subscription' && value !== 'discount') return
    if (value === typeFilter.value) return
    typeFilter.value = value
    selection.clear()
  } else if (key === 'status') {
    if (value !== '' && value !== 'active' && value !== 'inactive') return
    if (value === statusFilter.value) return
    statusFilter.value = value
    selection.clear()
  }
}

function removeChip(key: string) {
  if (key === 'keyword') {
    // 清空草稿走原有 watch，立即清空已应用关键词和选择，不另加防抖
    keyword.value = ''
  } else if (key === 'type') {
    typeFilter.value = ''
    selection.clear()
  } else if (key === 'status') {
    statusFilter.value = ''
    selection.clear()
  }
}

function clearFilters() {
  selection.clear()
  keyword.value = ''
  typeFilter.value = ''
  statusFilter.value = ''
}

function fmtTime(iso?: string | null) {
  return iso ? iso.slice(0, 10) : '—'
}

onMounted(load)
</script>

<template>
  <div class="admin-page">
    <PageHeader
      eyebrow="运营中心"
      title="旧版兑换码"
      description="老系统遗留的码：只查旧码，不再新发。用户兑换后直接生成奖励（积分 / 订阅时长 / 抵扣额度），并查看谁核销了它。新发放统一走「会员卡码」。列表为最近 200 个。"
    >
      <template #actions>
        <el-button :loading="loading" @click="load" :icon="RefreshCw">刷新</el-button>
      </template>
    </PageHeader>

    <div class="stat-row">
      <StatTile label="旧版兑换码" :value="codes.length" :icon="Ticket" hint="最近 200 个以内" />
      <StatTile label="启用中" :value="activeCount" :suffix="`/ ${codes.length}`" :icon="CheckCircle2" hint="停用的码不能再核销" />
      <StatTile label="累计核销" :value="usedSummary" suffix="次" :icon="Gift" hint="按每码使用次数求和" />
    </div>

    <SectionCard title="全部旧版兑换码" :icon="Gift" :meta="hasFilter ? `筛出 ${visibleCodes.length} / ${codes.length}` : ''" flush>
      <div class="list-bar">
        <FilterBar
          class="fb-grow"
          :search="{
            value: keyword,
            placeholder: '搜索兑换码 / 备注 / 核销人',
            mode: 'debounced',
            debounceMs: 0,
          }"
          :filters="filters"
          :applied-filters="appliedFilters"
          @update:search="keyword = $event"
          @filter-change="handleFilterChange"
          @remove-chip="removeChip"
          @clear="clearFilters"
        />
      </div>

      <DataTable
        :rows="visibleCodes"
        :columns="columns"
        :loading="loading"
        :error="loadError"
        empty="暂无旧版兑换码"
        empty-description="老系统的码只剩历史数据；新发放统一走「会员卡码」。"
        :has-active-filters="hasFilter"
        selectable
        v-model:selectedKeys="selectedKeys"
        :is-row-selectable="(row: ExchangeCodeRow) => row.is_active"
        @retry="load"
        @clear-filters="clearFilters"
      >
        <template #bulk-bar>
          <el-button type="warning" plain size="small" :loading="bulkBusy" @click="bulkDeactivate">批量停用</el-button>
        </template>
        <template #cell-code="{ row }">
          <span class="mono code">{{ row.code }}</span>
        </template>

        <template #cell-type="{ row }">
          <span class="au-badge" :class="row.type === 'points' ? 'au-badge-green' : row.type === 'subscription' ? 'au-badge-amber' : 'au-badge-info'">
            {{ row.type === 'points' ? '积分' : row.type === 'subscription' ? '订阅' : '折扣' }}
          </span>
        </template>

        <template #cell-reward="{ row }">{{ rewardText(row) }}</template>

        <template #cell-use_count="{ row }"><span class="mono">{{ row.use_count }}/{{ row.max_uses }}</span></template>

        <template #cell-expires_at="{ row }"><span class="mono">{{ fmtTime(row.expires_at) }}</span></template>

        <template #cell-note="{ row }">
          <span v-if="!row.note" class="muted">—</span>
          <span v-else>{{ row.note }}</span>
        </template>

        <template #cell-is_active="{ row }">
          <span class="au-badge" :class="row.is_active ? 'au-badge-green' : 'au-badge-muted'">
            {{ row.is_active ? '启用' : '停用' }}
          </span>
        </template>

        <template #cell-used_by="{ row }">
          <span v-if="!row.used_by?.length" class="muted">—</span>
          <span v-else class="muted">{{ usedNames(row) }}</span>
        </template>

        <template #cell-actions="{ row }">
          <el-button size="small" :type="row.is_active ? 'warning' : 'success'" :loading="toggleBusyId === row.id" @click="toggleCode(row)">
            {{ row.is_active ? '停用' : '启用' }}
          </el-button>
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
.list-bar .head-actions { justify-content: flex-end; width: auto; }

.code { letter-spacing: 0.06em; font-weight: 600; color: var(--au-text); }
.muted { color: var(--au-text-3); font-size: 12px; }

@media (max-width: 768px) {
  .list-bar { padding: 4px 16px 12px; }
}

@media (max-width: 640px) {
  .stat-row { grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; }
}
</style>
