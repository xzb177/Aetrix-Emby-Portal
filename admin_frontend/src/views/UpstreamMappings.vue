<script setup lang="ts">
/**
 * 上游映射列表 —— 专线 B 第二阶段，只读。
 * GET /api/admin/upstream-mappings：按本地条目 / 源过滤，limit/offset 分页。
 * evidence 为后端原样对象，本页只做 JSON 文本展示，不解析、不改写。
 */
import { computed, onMounted, ref } from 'vue'
import { RefreshCw, ArrowLeftRight } from 'lucide-vue-next'
import { fetchUpstreamMappings } from '@/api/admin'
import type { UpstreamMappingRow } from '@/types'
import { PageHeader, SectionCard } from '@/components/ui'
import DataTable from '@/components/DataTable.vue'
import type { DataColumn } from '@/components/DataTable.vue'
import FilterBar from '@/components/FilterBar.vue'
import type { AppliedFilter, FilterConfig } from '@/components/FilterBar.vue'

const columns: DataColumn[] = [
  { key: 'id', label: 'ID', width: 80, mobile: 'title' },
  { key: 'local_item_id', label: '本地条目', width: 110 },
  { key: 'source_id', label: '源', width: 90 },
  { key: 'upstream_item_id', label: '上游条目', minWidth: 160 },
  { key: 'confidence', label: '置信度', width: 90 },
  { key: 'evidence', label: '证据', minWidth: 220, mobile: 'hide' },
  { key: 'created_at', label: '创建时间', width: 170 },
  { key: 'updated_at', label: '更新时间', width: 170, mobile: 'hide' },
]

const PAGE_SIZE = 50
const items = ref<UpstreamMappingRow[]>([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)
const loadError = ref('')

const draft = ref({ local_item_id: '', source_id: '' })
const committed = ref({ local_item_id: '', source_id: '' })

const filterConfigs = computed<FilterConfig[]>(() => [
  { key: 'local_item_id', label: '本地条目 ID', type: 'custom', value: draft.value.local_item_id },
  { key: 'source_id', label: '源 ID', type: 'custom', value: draft.value.source_id },
])

const appliedFilters = computed<AppliedFilter[]>(() => {
  const filters: AppliedFilter[] = []
  if (committed.value.local_item_id) filters.push({ key: 'local_item_id', label: '本地条目 ID', text: committed.value.local_item_id })
  if (committed.value.source_id) filters.push({ key: 'source_id', label: '源 ID', text: committed.value.source_id })
  return filters
})

const hasActiveFilters = computed(() => appliedFilters.value.length > 0)

function parseId(text: string): number | undefined {
  const trimmed = text.trim()
  if (!trimmed) return undefined
  const n = Number(trimmed)
  return Number.isFinite(n) ? Math.trunc(n) : undefined
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const res = await fetchUpstreamMappings({
      local_item_id: parseId(committed.value.local_item_id),
      source_id: parseId(committed.value.source_id),
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
  const next = { local_item_id: draft.value.local_item_id.trim(), source_id: draft.value.source_id.trim() }
  if (next.local_item_id === committed.value.local_item_id && next.source_id === committed.value.source_id) return
  committed.value = next
  page.value = 1
  void load()
}

function removeFilter(key: string) {
  if (key === 'local_item_id') draft.value.local_item_id = ''
  if (key === 'source_id') draft.value.source_id = ''
  commitQuery()
}

function clearFilters() {
  draft.value = { local_item_id: '', source_id: '' }
  commitQuery()
}

function fmt(v: string | null): string {
  if (!v) return '—'
  return v.slice(0, 19).replace('T', ' ')
}

function evidenceText(v: unknown): string {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'string') return v || '—'
  try {
    return JSON.stringify(v)
  } catch {
    return String(v)
  }
}

onMounted(load)
</script>

<template>
  <div class="admin-page">
    <PageHeader
      eyebrow="媒体"
      title="上游映射列表"
      description="本地条目到上游条目的映射（只读）：按本地条目 ID、源 ID 过滤，置信度与证据原样展示。"
    >
      <template #actions>
        <el-button :loading="loading" type="primary" :icon="RefreshCw" @click="load">刷新</el-button>
      </template>
    </PageHeader>

    <SectionCard title="映射清单" :icon="ArrowLeftRight" :meta="loading ? '加载中…' : `共 ${total} 条`" flush>
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
          <template #filter-local_item_id>
            <el-input
              v-model="draft.local_item_id"
              placeholder="本地条目 ID（数字）"
              clearable
              @keyup.enter="commitQuery"
              @clear="removeFilter('local_item_id')"
            />
          </template>
          <template #filter-source_id>
            <el-input
              v-model="draft.source_id"
              placeholder="源 ID（数字）"
              clearable
              @keyup.enter="commitQuery"
              @clear="removeFilter('source_id')"
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
        empty="暂无上游映射"
        empty-description="还没有任何本地→上游映射，或当前筛选条件下没有结果。"
        @retry="load"
        @page-change="(p: number) => { page = p; load() }"
        @clear-filters="clearFilters"
      >
        <template #cell-local_item_id="{ row }"><span class="mono">{{ row.local_item_id }}</span></template>
        <template #cell-source_id="{ row }"><span class="mono">{{ row.source_id }}</span></template>
        <template #cell-upstream_item_id="{ row }"><span class="mono">{{ row.upstream_item_id }}</span></template>
        <template #cell-confidence="{ row }">{{ row.confidence === null || row.confidence === undefined ? '—' : row.confidence }}</template>
        <template #cell-evidence="{ row }"><span class="evidence">{{ evidenceText(row.evidence) }}</span></template>
        <template #cell-created_at="{ row }"><span class="mono">{{ fmt(row.created_at) }}</span></template>
        <template #cell-updated_at="{ row }"><span class="mono">{{ fmt(row.updated_at) }}</span></template>
      </DataTable>
    </SectionCard>
  </div>
</template>

<style scoped>
.list-bar { display: flex; padding: 4px 20px 10px; }
.fb-grow { flex: 1 1 auto; }
.mono { font-family: ui-monospace, monospace; }
.evidence { display: inline-block; max-width: 420px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; vertical-align: bottom; }
</style>
