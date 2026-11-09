<script setup lang="ts">
/**
 * 双盘审计台账（goal-38 审计第一步）：个人盘 + 公共贡献池的全程
 * 留痕汇成一个查询面（GET /api/admin/audit/events，只读）。
 *
 * 五路记录归一展示：转存/池决策留痕、池成员状态流水、池激励
 * 发奖、Drive 接口报错、个人盘起播取用事件（门禁被拒 / 副本
 * 选路 / 换线失败）。可按用户、池成员、绑定、条目、来源、动作
 * 类别、结果组与时间范围筛。
 *
 * 口径如实：Drive 报错只有账号不可逆哈希，没有用户/绑定/条目
 * 维度——按这三者筛时报错行不会出现，页头说明里写明。
 */
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { RefreshCw, ScrollText, Search } from 'lucide-vue-next'
import { EmptyState, PageHeader, SectionCard } from '@/components/ui'
import { parseServerTime } from '@/utils/serverTime'
import { toAdminError } from '@/utils/errorMessage'
import {
  fetchAuditEvents,
  type AuditEventRow,
} from '@/api/admin'

const SOURCE_OPTIONS = [
  { value: '', label: '全部来源' },
  { value: 'decision', label: '决策留痕' },
  { value: 'member_event', label: '池成员状态' },
  { value: 'reward', label: '池发奖' },
  { value: 'drive_error', label: 'Drive 报错' },
  { value: 'play_access', label: '起播取用' },
]

const CATEGORY_OPTIONS = [
  { value: '', label: '全部动作' },
  { value: 'transfer_gate', label: '转存门禁（个人盘）' },
  { value: 'pool_gate', label: '池门禁' },
  { value: 'pool_pick', label: '池选盘' },
  { value: 'pool_breaker', label: '池熔断' },
  { value: 'pool_evict', label: '池清理淘汰' },
  { value: 'member_status', label: '池成员状态变更' },
  { value: 'pool_reward', label: '池发奖' },
  { value: 'gate_reject', label: '起播门禁被拒' },
  { value: 'copy_resolve', label: '副本选路' },
  { value: 'switch_failed', label: '换线失败' },
]

const OUTCOME_OPTIONS = [
  { value: '', label: '全部结果' },
  { value: 'success', label: '放行 / 完成' },
  { value: 'rejected', label: '被拒 / 失败' },
  { value: 'in_progress', label: '处理中' },
  { value: 'changed', label: '状态变更' },
  { value: 'error', label: '报错' },
]

const OUTCOME_TEXT: Record<string, string> = {
  success: '放行 / 完成',
  rejected: '被拒 / 失败',
  in_progress: '处理中',
  changed: '状态变更',
  error: '报错',
}

const OUTCOME_CLASS: Record<string, string> = {
  success: 'outcome-ok',
  rejected: 'outcome-bad',
  in_progress: 'outcome-mid',
  changed: 'outcome-mut',
  error: 'outcome-bad',
}

const PAGE_SIZE = 50

const filters = reactive({
  source: '',
  category: '',
  outcome: '',
  userId: '',
  memberId: '',
  bindingId: '',
  itemId: '',
  start: '',
  end: '',
})

const rows = ref<AuditEventRow[]>([])
const total = ref(0)
const offset = ref(0)
const loading = ref(false)

function fmtTime(iso: string | null): string {
  if (!iso) return '—'
  const d = parseServerTime(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString()
}

function idOrUndef(text: string): number | undefined {
  const n = Number(text)
  return text.trim() !== '' && Number.isFinite(n) && n > 0 ? n : undefined
}

async function load() {
  loading.value = true
  try {
    const res = await fetchAuditEvents({
      source: filters.source || undefined,
      category: filters.category || undefined,
      outcome: filters.outcome || undefined,
      user_id: idOrUndef(filters.userId),
      member_id: idOrUndef(filters.memberId),
      binding_id: idOrUndef(filters.bindingId),
      item_id: idOrUndef(filters.itemId),
      start: filters.start || undefined,
      end: filters.end || undefined,
      limit: PAGE_SIZE,
      offset: offset.value,
    })
    rows.value = res.items
    total.value = res.total
  } catch (err) {
    ElMessage.error(toAdminError(err, '审计台账加载失败'))
    rows.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

function search() {
  offset.value = 0
  void load()
}

function resetFilters() {
  filters.source = ''
  filters.category = ''
  filters.outcome = ''
  filters.userId = ''
  filters.memberId = ''
  filters.bindingId = ''
  filters.itemId = ''
  filters.start = ''
  filters.end = ''
  search()
}

function prevPage() {
  if (offset.value <= 0) return
  offset.value = Math.max(0, offset.value - PAGE_SIZE)
  void load()
}

function nextPage() {
  if (offset.value + PAGE_SIZE >= total.value) return
  offset.value += PAGE_SIZE
  void load()
}

function detailText(row: AuditEventRow): string {
  if (!row.detail) return '—'
  try {
    return JSON.stringify(row.detail, null, 2)
  } catch {
    return String(row.detail)
  }
}

onMounted(() => void load())
</script>

<template>
  <div class="audit-page">
    <PageHeader
      eyebrow="媒体"
      title="双盘审计"
      description="个人盘与公共贡献池的全程留痕：谁的盘、何时、做了什么、成没成、为什么被拒。Drive 报错只有账号哈希、没有用户/绑定/条目维度，按这三者筛选时报错行不会出现。"
    />

    <SectionCard title="筛选">
      <div class="filter-grid">
        <el-select v-model="filters.source" placeholder="全部来源">
          <el-option
            v-for="opt in SOURCE_OPTIONS"
            :key="opt.value"
            :label="opt.label"
            :value="opt.value"
          />
        </el-select>
        <el-select v-model="filters.category" placeholder="全部动作">
          <el-option
            v-for="opt in CATEGORY_OPTIONS"
            :key="opt.value"
            :label="opt.label"
            :value="opt.value"
          />
        </el-select>
        <el-select v-model="filters.outcome" placeholder="全部结果">
          <el-option
            v-for="opt in OUTCOME_OPTIONS"
            :key="opt.value"
            :label="opt.label"
            :value="opt.value"
          />
        </el-select>
        <el-input v-model="filters.userId" placeholder="用户 ID" clearable />
        <el-input v-model="filters.memberId" placeholder="池成员 ID" clearable />
        <el-input v-model="filters.bindingId" placeholder="绑定 ID" clearable />
        <el-input v-model="filters.itemId" placeholder="条目 ID" clearable />
        <el-date-picker
          v-model="filters.start"
          type="datetime"
          value-format="YYYY-MM-DDTHH:mm:ss"
          placeholder="开始时间"
        />
        <el-date-picker
          v-model="filters.end"
          type="datetime"
          value-format="YYYY-MM-DDTHH:mm:ss"
          placeholder="结束时间（不含）"
        />
        <div class="filter-actions">
          <el-button type="primary" :loading="loading" @click="search">
            <Search :size="14" /> 查询
          </el-button>
          <el-button @click="resetFilters">
            <RefreshCw :size="14" /> 重置
          </el-button>
        </div>
      </div>
    </SectionCard>

    <SectionCard :title="`台账（共 ${total} 条）`">
      <el-table
        v-if="rows.length"
        :data="rows"
        size="small"
        class="audit-table"
        v-loading="loading"
      >
        <el-table-column type="expand">
          <template #default="{ row }">
            <pre class="detail-pre">{{ detailText(row) }}</pre>
          </template>
        </el-table-column>
        <el-table-column label="时间" width="168">
          <template #default="{ row }">
            <span class="mono">{{ fmtTime(row.occurred_at) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="动作" min-width="140">
          <template #default="{ row }">{{ row.action_label }}</template>
        </el-table-column>
        <el-table-column label="用户 / 成员 / 绑定 / 条目" min-width="190">
          <template #default="{ row }">
            <span class="mono dim">
              用户 {{ row.user_id ?? '—' }} · 成员 {{ row.member_id ?? '—' }}
              · 绑定 {{ row.binding_id ?? '—' }} · 条目 {{ row.item_id ?? '—' }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="结果" width="150">
          <template #default="{ row }">
            <span :class="OUTCOME_CLASS[row.outcome] || 'outcome-mut'">
              {{ OUTCOME_TEXT[row.outcome] || row.outcome }}
            </span>
            <span class="mono dim"> {{ row.result }}</span>
          </template>
        </el-table-column>
        <el-table-column label="原因" min-width="180">
          <template #default="{ row }">{{ row.reason || '—' }}</template>
        </el-table-column>
      </el-table>
      <EmptyState
        v-else-if="!loading"
        compact
        :icon="ScrollText"
        title="没有符合条件的记录"
        description="换个筛选条件试试；起播取用类记录从本功能上线后开始积累。"
      />
      <div class="pager">
        <el-button :disabled="offset <= 0 || loading" @click="prevPage">
          上一页
        </el-button>
        <span class="dim">
          第 {{ Math.floor(offset / PAGE_SIZE) + 1 }} 页 · 本页 {{ rows.length }} 条
        </span>
        <el-button
          :disabled="offset + PAGE_SIZE >= total || loading"
          @click="nextPage"
        >
          下一页
        </el-button>
      </div>
    </SectionCard>
  </div>
</template>

<style scoped>
.audit-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.filter-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
  align-items: center;
}

.filter-actions {
  display: flex;
  gap: 8px;
}

.audit-table {
  width: 100%;
}

.mono {
  font-family: var(--au-font-mono, monospace);
  font-size: 12px;
}

.dim {
  color: var(--au-text-3);
}

.detail-pre {
  margin: 0;
  padding: 8px 12px;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--au-text-2);
}

.outcome-ok { color: var(--au-success); }
.outcome-bad { color: var(--au-danger); }
.outcome-mid { color: var(--au-warning); }
.outcome-mut { color: var(--au-text-2); }

.pager {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 12px;
}
</style>
