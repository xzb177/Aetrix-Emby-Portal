<script setup lang="ts">
/** 探险世界 · 到此一游留言墙管理：查看最新留言 / 删除（软删除=隐藏） */
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { toAdminError } from '@/utils/errorMessage'
import { parseServerTime } from '@/utils/serverTime'
import { MessageSquareText, RefreshCw } from 'lucide-vue-next'
import { PageHeader, SectionCard } from '@/components/ui'
import { fetchWallPosts, deleteWallPost } from '@/api/admin'
import type { WallPost } from '@/api/admin'
import DataTable from '@/components/DataTable.vue'
import type { DataColumn } from '@/components/DataTable.vue'

const columns: DataColumn[] = [
  { key: 'nickname', label: '用户', width: 130 },
  { key: 'content', label: '留言', minWidth: 260, mobile: 'title' },
  { key: 'is_hidden', label: '状态', width: 90 },
  { key: 'created_at', label: '留言时间', width: 160 },
  { key: 'actions', label: '操作', width: 110, fixed: 'right', align: 'right' },
]

const list = ref<WallPost[]>([])
const loading = ref(false)
const loadError = ref('')
const rowBusyId = ref<number | null>(null)

const visibleList = computed(() => list.value)

function statusLabel(p: WallPost): string {
  return p.is_hidden ? '已删除' : '展示中'
}

function fmtTime(v: string | null): string {
  if (!v) return '—'
  const d = parseServerTime(v)
  if (Number.isNaN(d.getTime())) return '—'
  const p2 = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())} ${p2(d.getHours())}:${p2(d.getMinutes())}`
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    list.value = await fetchWallPosts(100)
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : '加载失败'
  } finally {
    loading.value = false
  }
}

async function onDelete(p: WallPost) {
  if (p.is_hidden || rowBusyId.value !== null) return
  try {
    await ElMessageBox.confirm(
      `确定删除「${p.nickname}」的这条留言吗？删除后用户端不再展示。`,
      '删除留言',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  rowBusyId.value = p.id
  try {
    await deleteWallPost(p.id)
    p.is_hidden = true
    ElMessage.success('已删除')
  } catch (e) {
    ElMessage.error(toAdminError(e))
  } finally {
    rowBusyId.value = null
  }
}

onMounted(load)
</script>

<template>
  <div class="admin-page">
    <PageHeader
      title="到此一游留言墙"
      desc="探险世界留言墙的最新留言；删除为软删除（隐藏），用户端即时不再展示。"
      class="mb-4"
    >
      <template #icon><MessageSquareText :size="20" /></template>
      <template #actions>
        <el-button :loading="loading" :icon="RefreshCw" @click="load">刷新</el-button>
      </template>
    </PageHeader>

    <SectionCard>
      <DataTable
        :columns="columns"
        :rows="visibleList"
        :loading="loading"
        :error="loadError"
        empty="还没有留言"
        @retry="load"
      >
        <template #cell-is_hidden="{ row }">{{ statusLabel(row as WallPost) }}</template>
        <template #cell-created_at="{ row }">{{ fmtTime((row as WallPost).created_at) }}</template>
        <template #cell-actions="{ row }">
          <el-button
            v-if="!(row as WallPost).is_hidden"
            size="small"
            type="danger"
            :loading="rowBusyId === (row as WallPost).id"
            @click="onDelete(row as WallPost)"
          >删除</el-button>
        </template>
      </DataTable>
    </SectionCard>
  </div>
</template>
