<script setup lang="ts">
/**
 * 存储绑定列表 —— 专线 B 第二阶段，只读。
 * GET /api/admin/storage-bindings：按用户 id / 状态过滤，limit/offset 分页。
 * 凭据只出 has_credential，本页不展示、不请求任何凭据原文；只有刷新（重新 GET）。
 */
import { computed, onMounted, ref } from 'vue'
import { RefreshCw, HardDrive } from 'lucide-vue-next'
import { fetchStorageBindings } from '@/api/admin'
import type { StorageBindingRow } from '@/types'
import { PageHeader, SectionCard } from '@/components/ui'
import DataTable from '@/components/DataTable.vue'
import type { DataColumn } from '@/components/DataTable.vue'
import FilterBar from '@/components/FilterBar.vue'
import type { AppliedFilter, FilterConfig } from '@/components/FilterBar.vue'

const columns: DataColumn[] = [
  { key: 'id', label: 'ID', width: 80, mobile: 'title' },
  { key: 'user_id', label: '用户 ID', width: 90 },
  { key: 'provider', label: '服务商', width: 110 },
  { key: 'account_label', label: '账号', minWidth: 140 },
  { key: 'status', label: '状态', width: 110 },
  { key: 'has_credential', label: '有凭据', width: 80 },
  { key: 'last_verified_at', label: '最近验证', width: 170, mobile: 'hide' },
  { key: 'last_error_kind', label: '最近错误', width: 130, mobile: 'hide' },
  { key: 'error_message', label: '错误说明', minWidth: 200, mobile: 'hide' },
  { key: 'created_at', label: '创建时间', width: 170 },
]

const PAGE_SIZE = 50
const items = ref<StorageBindingRow[]>([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)
const loadError = ref('')

const draft = ref({ user_id: '', status: '' })
const committed = ref({ user_id: '', status: '' })

const filterConfigs = computed<FilterConfig[]>(() => [
  { key: 'user_id', label: '用户 ID', type: 'custom', value: draft.value.user_id },
  { key: 'status', label: '状态', type: 'custom', value: draft.value.status },
])

const appliedFilters = computed<AppliedFilter[]>(() => {
  const filters: AppliedFilter[] = []
  if (committed.value.user_id) filters.push({ key: 'user_id', label: '用户 ID', text: committed.value.user_id })
  if (committed.value.status) filters.push({ key: 'status', label: '状态', text: committed.value.status })
  return filters
})

const hasActiveFilters = computed(() => appliedFilters.value.length > 0)

function parseUserId(text: string): number | undefined {
  const trimmed = text.trim()
  if (!trimmed) return undefined
  const n = Number(trimmed)
  return Number.isFinite(n) ? Math.trunc(n) : undefined
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const res = await fetchStorageBindings({
      user_id: parseUserId(committed.value.user_id),
      status: committed.value.status.trim() || undefined,
      limit: PAGE_SIZE,
      offset: (page.value - 1) * PAGE_SIZE,
    })
    items.value = res.items
    total.value = res.total
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : '加载失败'
  } finally {
    loading.value = false
  }
}

function commitQuery() {
  const next = { user_id: draft.value.user_id.trim(), status: draft.value.status.trim() }
  if (next.user_id === committed.value.user_id && next.status === committed.value.status) return
  committed.value = next
  page.value = 1
  void load()
}

function removeFilter(key: string) {
  if (key === 'user_id') draft.value.user_id = ''
  if (key === 'status') draft.value.status = ''
  commitQuery()
}

function clearFilters() {
  draft.value = { user_id: '', status: '' }
  commitQuery()
}

function fmt(v: string | null): string {
  if (!v) return '—'
  return v.slice(0, 19).replace('T', ' ')
}

onMounted(load)
</script>

<template>
  <div class="admin-page">
    <PageHeader
      eyebrow="媒体"
      title="存储绑定列表"
      description="全站用户存储绑定（只读）：按用户 ID、状态过滤；凭据只显示有没有，不展示原文。"
    >
      <template #actions>
        <el-button :loading="loading" type="primary" :icon="RefreshCw" @click="load">刷新</el-button>
      </template>
    </PageHeader>

    <SectionCard title="绑定清单" :icon="HardDrive" :meta="loading ? '加载中…' : `共 ${total} 条`" flush>
      <div class="list-bar">
        <FilterBar
          class="fb-grow"
          :filters="filterConfigs"
          :applied-filters="appliedFilters"
          :query-button="true"
          :loading="loading"
          @query="commitQuery"
          @remove-chip="removeFilter"
          @clear="clearFilters"
        >
          <template #filter-user_id>
            <el-input
              v-model="draft.user_id"
              placeholder="用户 ID（数字）"
              clearable
              @keyup.enter="commitQuery"
              @clear="removeFilter('user_id')"
            />
          </template>
          <template #filter-status>
            <el-input
              v-model="draft.status"
              placeholder="状态"
              clearable
              @keyup.enter="commitQuery"
              @clear="removeFilter('status')"
            />
          </template>
        </FilterBar>
      </div>

      <DataTable
        :rows="items"
        :columns="columns"
        :loading="loading"
        :error="loadError"
        :pagination="{ total, page, pageSize: PAGE_SIZE }"
        :has-active-filters="hasActiveFilters"
        empty="暂无存储绑定"
        empty-description="还没有任何绑定，或当前筛选条件下没有结果。"
        @retry="load"
        @page-change="(p: number) => { page = p; load() }"
        @clear-filters="clearFilters"
      >
        <template #cell-user_id="{ row }"><span class="mono">{{ row.user_id }}</span></template>
        <template #cell-account_label="{ row }">{{ row.account_label || '—' }}</template>
        <template #cell-status="{ row }">
          <span class="au-badge" :class="row.status === 'active' ? 'au-badge-green' : 'au-badge-muted'">{{ row.status }}</span>
        </template>
        <template #cell-has_credential="{ row }">{{ row.has_credential ? '是' : '否' }}</template>
        <template #cell-last_verified_at="{ row }"><span class="mono">{{ fmt(row.last_verified_at) }}</span></template>
        <template #cell-last_error_kind="{ row }">{{ row.last_error_kind || '—' }}</template>
        <template #cell-error_message="{ row }">{{ row.error_message || '—' }}</template>
        <template #cell-created_at="{ row }"><span class="mono">{{ fmt(row.created_at) }}</span></template>
      </DataTable>
    </SectionCard>
  </div>
</template>

<style scoped>
.list-bar { display: flex; padding: 4px 20px 10px; }
.fb-grow { flex: 1 1 auto; }
.mono { font-family: ui-monospace, monospace; }
</style>
