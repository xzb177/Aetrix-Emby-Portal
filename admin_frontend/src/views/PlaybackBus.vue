<script setup lang="ts">
/**
 * 播放总线 —— 专线 B 第二阶段，只读展示，绝不加发命令的写操作。
 * GET /api/admin/playback-bus/snapshot：无快照时后端回 404，本页显示空状态，不炸成错误页。
 * GET /api/admin/playback-bus/commands：最近命令审计行数组。
 * 两路独立加载：一路失败不连累另一路。
 */
import { onMounted, ref } from 'vue'
import { RefreshCw, Radio, ListOrdered } from 'lucide-vue-next'
import { fetchPlaybackBusCommands, fetchPlaybackBusSnapshot } from '@/api/admin'
import type { PlaybackBusCommand, PlaybackBusSnapshot } from '@/types'
import { EmptyState, PageHeader, SectionCard, StatTile } from '@/components/ui'
import DataTable from '@/components/DataTable.vue'
import type { DataColumn } from '@/components/DataTable.vue'

const sessionColumns: DataColumn[] = [
  { key: 'session_key', label: '会话', minWidth: 180, mobile: 'title' },
  { key: 'user_id', label: '用户 ID', width: 90 },
  { key: 'item_id', label: '条目 ID', width: 100 },
  { key: 'variant', label: '变体', width: 110, mobile: 'hide' },
  { key: 'device_id', label: '设备', minWidth: 120, mobile: 'hide' },
  { key: 'position_ticks', label: '进度 ticks', width: 120, mobile: 'hide' },
  { key: 'is_paused', label: '已暂停', width: 80 },
  { key: 'last_command_type', label: '最近命令', width: 110, mobile: 'hide' },
  { key: 'last_command_status', label: '命令状态', width: 120, mobile: 'hide' },
  { key: 'last_update_at', label: '最近更新', width: 170 },
]

const commandColumns: DataColumn[] = [
  { key: 'command_id', label: '命令 ID', minWidth: 160, mobile: 'title' },
  { key: 'session_key', label: '会话', minWidth: 160 },
  { key: 'device_id', label: '设备', minWidth: 120, mobile: 'hide' },
  { key: 'command', label: '命令', width: 100 },
  { key: 'payload', label: '参数', minWidth: 160, mobile: 'hide' },
  { key: 'status', label: '状态', width: 110 },
  { key: 'outcome', label: '结果', width: 130, mobile: 'hide' },
  { key: 'requested_by', label: '发起人', width: 90, mobile: 'hide' },
  { key: 'requested_at', label: '请求时间', width: 170 },
  { key: 'sent_at', label: '发送时间', width: 170, mobile: 'hide' },
  { key: 'delivered_at', label: '送达时间', width: 170, mobile: 'hide' },
  { key: 'completed_at', label: '完成时间', width: 170, mobile: 'hide' },
  { key: 'error', label: '错误', minWidth: 160, mobile: 'hide' },
]

const snapshot = ref<PlaybackBusSnapshot | null>(null)
const snapshotMissing = ref(false)
const snapshotLoading = ref(false)
const snapshotError = ref('')
const commands = ref<PlaybackBusCommand[]>([])
const commandsLoading = ref(false)
const commandsError = ref('')
const commandLimit = ref(50)

function isNotFound(e: unknown): boolean {
  return (e as { response?: { status?: number } }).response?.status === 404
}

async function loadSnapshot() {
  snapshotLoading.value = true
  snapshotError.value = ''
  snapshotMissing.value = false
  try {
    snapshot.value = await fetchPlaybackBusSnapshot()
  } catch (e) {
    // 无快照是正常空状态，不是错误页；其他失败才进错误态
    if (isNotFound(e)) {
      snapshot.value = null
      snapshotMissing.value = true
    } else {
      snapshotError.value = e instanceof Error ? e.message : '加载失败'
    }
  } finally {
    snapshotLoading.value = false
  }
}

async function loadCommands() {
  commandsLoading.value = true
  commandsError.value = ''
  try {
    commands.value = await fetchPlaybackBusCommands(commandLimit.value)
  } catch (e) {
    commandsError.value = e instanceof Error ? e.message : '加载失败'
  } finally {
    commandsLoading.value = false
  }
}

function load() {
  void loadSnapshot()
  void loadCommands()
}

function fmt(v: string | null): string {
  if (!v) return '—'
  return v.slice(0, 19).replace('T', ' ')
}

function payloadText(v: unknown): string {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'string') return v || '—'
  try {
    return JSON.stringify(v)
  } catch {
    return String(v)
  }
}

function sessionCount(s: PlaybackBusSnapshot | null): number {
  return s ? s.sessions.length : 0
}

onMounted(load)
</script>

<template>
  <div class="admin-page">
    <PageHeader
      eyebrow="系统与审计"
      title="播放总线"
      description="播放实时总线的最新快照与最近命令审计（只读展示）；本页不提供发命令入口。"
    >
      <template #actions>
        <el-button :loading="snapshotLoading || commandsLoading" type="primary" :icon="RefreshCw" @click="load">刷新</el-button>
      </template>
    </PageHeader>

    <div class="stat-row">
      <StatTile
        label="快照会话数"
        :value="snapshotMissing ? '无快照' : sessionCount(snapshot)"
        :icon="Radio"
        :hint="snapshot ? `快照计算于 ${fmt(snapshot.computed_at)}` : snapshotMissing ? '后端暂无快照（404 属正常空状态）' : '加载中…'"
      />
      <StatTile label="命令条数" :value="commands.length" :icon="ListOrdered" :hint="`最近 ${commandLimit} 条命令审计`" />
    </div>

    <SectionCard title="最新快照" :icon="Radio" :meta="snapshot ? `计算于 ${fmt(snapshot.computed_at)}` : snapshotMissing ? '暂无快照' : ''" flush>
      <div v-if="snapshotError">
        <EmptyState title="快照加载失败" :description="snapshotError">
          <template #actions><el-button :loading="snapshotLoading" @click="loadSnapshot">重试</el-button></template>
        </EmptyState>
      </div>
      <div v-else-if="snapshotMissing">
        <EmptyState title="暂无播放快照" description="后端还没有写出快照（无进行中会话、快照过期或总线未开启时都会这样），不是故障；有会话在播时这里会出现。" />
      </div>
      <DataTable
        v-else
        :rows="snapshot ? snapshot.sessions : []"
        :columns="sessionColumns"
        :loading="snapshotLoading"
        empty="快照里没有进行中会话"
        empty-description="当前没有进行中的播放会话。"
      >
        <template #cell-session_key="{ row }"><span class="mono">{{ row.session_key }}</span></template>
        <template #cell-user_id="{ row }">{{ row.user_id ?? '—' }}</template>
        <template #cell-item_id="{ row }">{{ row.item_id ?? '—' }}</template>
        <template #cell-variant="{ row }">{{ row.variant || '—' }}</template>
        <template #cell-device_id="{ row }">{{ row.device_id || '—' }}</template>
        <template #cell-is_paused="{ row }">{{ row.is_paused ? '是' : '否' }}</template>
        <template #cell-last_command_type="{ row }">{{ row.last_command_type || '—' }}</template>
        <template #cell-last_command_status="{ row }">{{ row.last_command_status || '—' }}</template>
        <template #cell-last_update_at="{ row }"><span class="mono">{{ fmt(row.last_update_at) }}</span></template>
      </DataTable>
    </SectionCard>

    <SectionCard title="最近命令" :icon="ListOrdered" :meta="commands.length ? `${commands.length} 条` : ''" flush>
      <div class="list-bar">
        <el-select v-model="commandLimit" class="w-limit" aria-label="命令条数" @change="loadCommands">
          <el-option :label="'最近 20 条'" :value="20" />
          <el-option :label="'最近 50 条'" :value="50" />
          <el-option :label="'最近 100 条'" :value="100" />
        </el-select>
        <span class="muted">只读审计列表；发命令请走服务端接口，本页不提供入口。</span>
      </div>
      <DataTable
        :rows="commands"
        :columns="commandColumns"
        :loading="commandsLoading"
        :error="commandsError"
        empty="暂无播放命令"
        empty-description="还没有任何播放控制命令记录。"
        @retry="loadCommands"
      >
        <template #cell-command_id="{ row }"><span class="mono">{{ row.command_id }}</span></template>
        <template #cell-session_key="{ row }"><span class="mono">{{ row.session_key }}</span></template>
        <template #cell-device_id="{ row }">{{ row.device_id || '—' }}</template>
        <template #cell-requested_by="{ row }">{{ row.requested_by ?? '—' }}</template>
        <template #cell-requested_at="{ row }"><span class="mono">{{ fmt(row.requested_at) }}</span></template>
        <template #cell-sent_at="{ row }"><span class="mono">{{ fmt(row.sent_at) }}</span></template>
        <template #cell-delivered_at="{ row }"><span class="mono">{{ fmt(row.delivered_at) }}</span></template>
        <template #cell-completed_at="{ row }"><span class="mono">{{ fmt(row.completed_at) }}</span></template>
        <template #cell-error="{ row }">{{ row.error || '—' }}</template>
        <template #cell-payload="{ row }">{{ payloadText(row.payload) }}</template>
      </DataTable>
    </SectionCard>
  </div>
</template>

<style scoped>
.stat-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin-bottom: 16px; }
.list-bar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; padding: 4px 20px 10px; }
.w-limit { width: 140px; }
.muted { color: var(--au-text-3); font-size: 12px; }
.mono { font-family: ui-monospace, monospace; }
</style>
