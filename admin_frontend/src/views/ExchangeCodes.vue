<script setup lang="ts">
/**
 * 兑换码管理：批量生成（积分/订阅/折扣型）、停用/启用、使用审计
 */
import { onMounted, onUnmounted, ref, computed, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Gift, Plus, RefreshCw, CheckCircle2, Ticket } from 'lucide-vue-next'
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
  createExchangeCodes,
  updateExchangeCode,
  fetchEconomyPlans,
  type ExchangeCodeRow,
  type PlanRowFull,
} from '@/api/economy'

const loading = ref(false)
const loadError = ref('')
const codes = ref<ExchangeCodeRow[]>([])
const plans = ref<PlanRowFull[]>([])

// 生成表单
const genVisible = ref(false)
const genForm = ref({
  count: 10,
  type: 'points' as 'points' | 'subscription' | 'discount',
  points_value: 50,
  plan_id: undefined as number | undefined,
  duration_days: 30,
  discount_pct: 85,
  max_uses: 1,
  expires_days: 30,
  note: '',
})
const genLoading = ref(false)
const genResult = ref<string[]>([])
const resultVisible = ref(false)

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const [c, p] = await Promise.all([
      fetchExchangeCodes({ limit: 200 }),
      fetchEconomyPlans().catch(() => ({ plans: [] })),
    ])
    codes.value = c.codes
    plans.value = (p as { plans: PlanRowFull[] }).plans
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : '加载失败'
  } finally {
    loading.value = false
  }
}

async function handleGenerate() {
  if (genLoading.value) return
  if (genForm.value.type === 'subscription' && !genForm.value.plan_id) {
    ElMessage.warning('订阅型兑换码要先选择套餐')
    return
  }
  genLoading.value = true
  try {
    const res = await createExchangeCodes({
      count: genForm.value.count,
      type: genForm.value.type,
      points_value: genForm.value.type === 'points' ? genForm.value.points_value : undefined,
      plan_id: genForm.value.type === 'subscription' ? genForm.value.plan_id : undefined,
      duration_days: genForm.value.type === 'subscription' ? genForm.value.duration_days : undefined,
      discount_pct: genForm.value.type === 'discount' ? genForm.value.discount_pct : undefined,
      max_uses: genForm.value.max_uses,
      expires_days: genForm.value.expires_days,
      note: genForm.value.note || undefined,
    })
    genResult.value = res.codes.map(c => c.code)
    resultVisible.value = true
    genVisible.value = false
    ElMessage.success(`成功生成 ${genResult.value.length} 个兑换码`)
    load()
  } catch {
    /* 写操作失败由请求拦截器统一弹错，这里不再重复提示 */
  } finally {
    genLoading.value = false
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

/** 剪贴板只在安全上下文（https / localhost）可用：http 部署下 writeText 会直接 reject */
async function copyAll() {
  try {
    await navigator.clipboard.writeText(genResult.value.join('\n'))
    ElMessage.success('已复制全部')
  } catch {
    ElMessage.warning('浏览器不允许写入剪贴板，请在文本框里全选后手动复制')
  }
}

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
      title="兑换码"
      description="批量生成积分 / 订阅兑换码，停用或启用单个码，并查看谁核销了它。列表为最近 200 个。"
    >
      <template #actions>
        <el-button :loading="loading" @click="load" :icon="RefreshCw">刷新</el-button>
        <el-button type="primary" @click="genVisible = true" :icon="Plus">批量生成</el-button>
      </template>
    </PageHeader>

    <el-alert
      type="info"
      :closable="false"
      show-icon
      title="分工说明：兑换码兑换后生成奖励（积分 / 订阅时长 / 抵扣额度）；和「优惠券」不同——优惠券是在下单支付那一刻直接抵扣的。"
    />

    <div class="stat-row">
      <StatTile label="兑换码" :value="codes.length" :icon="Ticket" hint="最近 200 个以内" />
      <StatTile label="启用中" :value="activeCount" :suffix="`/ ${codes.length}`" :icon="CheckCircle2" hint="停用的码不能再核销" />
      <StatTile label="累计核销" :value="usedSummary" suffix="次" :icon="Gift" hint="按每码使用次数求和" />
    </div>

    <SectionCard title="全部兑换码" :icon="Gift" :meta="hasFilter ? `筛出 ${visibleCodes.length} / ${codes.length}` : ''" flush>
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
        empty="暂无兑换码"
        empty-description="点右上角「批量生成」做第一批。"
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

    <!-- 生成对话框 -->
    <el-dialog v-model="genVisible" title="批量生成兑换码" width="min(480px, 92vw)">
      <el-form label-position="top">
        <el-form-item label="类型">
          <el-radio-group v-model="genForm.type">
            <el-radio-button value="points">积分</el-radio-button>
            <el-radio-button value="subscription">订阅</el-radio-button>
            <el-radio-button value="discount">折扣</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="genForm.type === 'points'" label="积分数">
          <el-input-number v-model="genForm.points_value" :min="1" :max="100000" style="width: 100%" />
        </el-form-item>
        <el-form-item v-else-if="genForm.type === 'discount'" label="折扣（实付百分比）">
          <el-input-number v-model="genForm.discount_pct" :min="1" :max="99" style="width: 100%" />
          <div class="form-hint">85 = 八五折，用户下次购买订阅实付 85%</div>
        </el-form-item>
        <template v-else>
          <el-form-item label="套餐">
            <el-select v-model="genForm.plan_id" placeholder="选择订阅套餐" style="width: 100%">
              <el-option v-for="p in plans" :key="p.id" :label="p.name" :value="p.id" />
              <template #empty>
                <div class="select-empty">还没有订阅套餐，先去「订阅套餐」建一个</div>
              </template>
            </el-select>
          </el-form-item>
          <el-form-item label="时长（天）">
            <el-input-number v-model="genForm.duration_days" :min="1" :max="3650" style="width: 100%" />
          </el-form-item>
        </template>
        <el-form-item label="数量">
          <el-input-number v-model="genForm.count" :min="1" :max="100" style="width: 100%" />
        </el-form-item>
        <el-form-item label="每码次数">
          <el-input-number v-model="genForm.max_uses" :min="1" :max="1000" style="width: 100%" />
        </el-form-item>
        <el-form-item label="有效天数">
          <el-input-number v-model="genForm.expires_days" :min="1" :max="3650" style="width: 100%" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="genForm.note" placeholder="活动名称等（选填）" maxlength="60" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="genVisible = false">取消</el-button>
        <el-button type="primary" :loading="genLoading" @click="handleGenerate">生成</el-button>
      </template>
    </el-dialog>

    <!-- 生成结果 -->
    <el-dialog v-model="resultVisible" title="生成结果（请保存）" width="min(420px, 92vw)">
      <el-input
        :model-value="genResult.join('\n')"
        type="textarea"
        :rows="10"
        readonly
        class="mono"
      />
      <template #footer>
        <el-button type="primary" @click="copyAll">复制全部</el-button>
      </template>
    </el-dialog>
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
.select-empty { padding: 10px 12px; color: var(--au-text-3); font-size: 12px; }

@media (max-width: 768px) {
  .list-bar { padding: 4px 16px 12px; }
}

@media (max-width: 640px) {
  .stat-row { grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; }
}
</style>
