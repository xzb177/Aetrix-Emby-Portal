<script setup lang="ts">
/**
 * 139 账号池 —— 专线 B 第二阶段，只读。
 * GET /api/admin/yun139/pools：池 + 成员同页，limit/offset 分页。
 * 成员只出 has_credential，本页不展示、不请求任何凭据原文。
 */
import { onMounted, ref } from 'vue'
import { RefreshCw, Users } from 'lucide-vue-next'
import { fetchYun139Pools } from '@/api/admin'
import type { Yun139Pool } from '@/types'
import { EmptyState, PageHeader, SectionCard } from '@/components/ui'
import DataTable from '@/components/DataTable.vue'
import type { DataColumn } from '@/components/DataTable.vue'

const poolColumns: DataColumn[] = [
  { key: 'id', label: 'ID', width: 80, mobile: 'title' },
  { key: 'name', label: '池名', minWidth: 140 },
  { key: 'strategy', label: '策略', width: 120 },
  { key: 'cooldown_seconds', label: '冷却（秒）', width: 110 },
  { key: 'max_concurrent', label: '最大并发', width: 110 },
  { key: 'member_count', label: '成员数', width: 90 },
  { key: 'created_at', label: '创建时间', width: 170 },
]

const memberColumns: DataColumn[] = [
  { key: 'id', label: 'ID', width: 80, mobile: 'title' },
  { key: 'label', label: '成员', minWidth: 140 },
  { key: 'weight', label: '权重', width: 90 },
  { key: 'fail_count', label: '失败次数', width: 100 },
  { key: 'has_credential', label: '有凭据', width: 90 },
  { key: 'cooldown_until', label: '冷却至', width: 170, mobile: 'hide' },
  { key: 'created_at', label: '创建时间', width: 170 },
]

const PAGE_SIZE = 50
const items = ref<Yun139Pool[]>([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)
const loadError = ref('')

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const res = await fetchYun139Pools({
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
      title="139 账号池"
      description="139 账号池与池内成员（只读）：成员只显示有没有凭据，不展示凭据原文。"
    >
      <template #actions>
        <el-button :loading="loading" type="primary" :icon="RefreshCw" @click="load">刷新</el-button>
      </template>
    </PageHeader>

    <SectionCard title="账号池" :icon="Users" :meta="loading ? '加载中…' : `共 ${total} 个池`" flush>
      <DataTable
        :rows="items"
        :columns="poolColumns"
        :loading="loading"
        :error="loadError"
        :pagination="{ total, page, pageSize: PAGE_SIZE }"
        empty="暂无 139 账号池"
        empty-description="还没有建任何 139 账号池。"
        @retry="load"
        @page-change="(p: number) => { page = p; load() }"
      >
        <template #cell-name="{ row }">{{ row.name }}</template>
        <template #cell-strategy="{ row }">{{ row.strategy }}</template>
        <template #cell-created_at="{ row }"><span class="mono">{{ fmt(row.created_at) }}</span></template>
      </DataTable>
    </SectionCard>

    <template v-if="items.length">
      <SectionCard
        v-for="pool in items"
        :key="pool.id"
        :title="`池内成员：${pool.name}`"
        :icon="Users"
        :meta="`${pool.member_count} 名成员`"
        flush
      >
        <DataTable
          v-if="pool.members.length"
          :rows="pool.members"
          :columns="memberColumns"
          empty="该池暂无成员"
        >
          <template #cell-label="{ row }">{{ row.label || '—' }}</template>
          <template #cell-has_credential="{ row }">{{ row.has_credential ? '是' : '否' }}</template>
          <template #cell-cooldown_until="{ row }"><span class="mono">{{ fmt(row.cooldown_until) }}</span></template>
          <template #cell-created_at="{ row }"><span class="mono">{{ fmt(row.created_at) }}</span></template>
        </DataTable>
        <EmptyState v-else compact title="该池暂无成员" />
      </SectionCard>
    </template>
  </div>
</template>

<style scoped>
.mono { font-family: ui-monospace, monospace; }
</style>
