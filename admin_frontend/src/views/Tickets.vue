<script setup lang="ts">
/** 工单管理：列表筛选/回复/关闭，回复联动站内通知 */
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Inbox, RefreshCw, Send, Ticket } from 'lucide-vue-next'
import { EmptyState, PageHeader, SectionCard } from '@/components/ui'
import { closeTicket, fetchTicketMessages, fetchTickets, replyTicket, updateTicket } from '@/api/admin'
import type { TicketMessageRow, TicketRow } from '@/types'
import { useQueryFilter } from '@/composables/useQueryFilter'
import DataTable from '@/components/DataTable.vue'
import type { DataColumn } from '@/components/DataTable.vue'

const columns: DataColumn[] = [
  { key: 'title', label: '工单', minWidth: 240, mobile: 'title' },
  { key: 'user_name', label: '用户', width: 120 },
  { key: 'category', label: '分类', width: 90, mobile: 'hide' },
  { key: 'priority', label: '优先级', width: 90 },
  { key: 'status', label: '状态', width: 100 },
  { key: 'updated_at', label: '更新时间', width: 150 },
  { key: 'actions', label: '操作', width: 110, fixed: 'right', align: 'right' },
]

const list = ref<TicketRow[]>([])
const loading = ref(false)
const loadError = ref('')
const statusFilter = ref('')
// 深链：仪表盘「待处理工单」/ 命令面板跳过来时带的就是这个筛选（Phase 5）
useQueryFilter(statusFilter, 'status', load)

const drawerVisible = ref(false)
const current = ref<TicketRow | null>(null)
const messages = ref<TicketMessageRow[]>([])
// 抽屉先开、消息后到：不给这个状态的话，拉取期间会先闪一下「两个管理员之间的空白」
const detailLoading = ref(false)
const detailError = ref(false)
const replyText = ref('')
const sending = ref(false)
/** 行内「关闭」动作进行中（按钮 loading，防重复点击） */
const rowBusyId = ref<number | null>(null)
const metaSaving = ref(false)

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    list.value = await fetchTickets(statusFilter.value ? { status_filter: statusFilter.value } : {})
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : '加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(load)

async function openDetail(t: TicketRow) {
  current.value = t
  messages.value = []
  replyText.value = ''
  drawerVisible.value = true
  detailLoading.value = true
  detailError.value = false
  try {
    messages.value = await fetchTicketMessages(t.id)
  } catch {
    // 拦截器已提示；区分「读取失败」与「还没有对话」
    detailError.value = true
  } finally {
    detailLoading.value = false
  }
}

async function send(closeAfter: boolean) {
  if (!current.value || !replyText.value.trim()) return
  sending.value = true
  try {
    await replyTicket(current.value.id, { message: replyText.value, close_ticket: closeAfter })
    ElMessage.success(closeAfter ? '已回复并关闭工单' : '回复成功')
    drawerVisible.value = false
    load()
  } catch {
    // 拦截器已提示；保留已输入的回复
  } finally {
    sending.value = false
  }
}

async function close(t: TicketRow) {
  try {
    await ElMessageBox.confirm(
      `关闭「${t.title}」后用户不能再回复，确定吗？`,
      '关闭工单',
      { type: 'warning' },
    )
  } catch {
    return // 用户点了取消
  }
  rowBusyId.value = t.id
  try {
    await closeTicket(t.id)
    ElMessage.success('工单已关闭')
    if (current.value?.id === t.id) current.value.status = 'closed'
    load()
  } catch {
    // 拦截器已提示
  } finally {
    rowBusyId.value = null
  }
}

/** 抽屉内直接调整状态 / 优先级（后端 PUT /tickets/{id}） */
async function patchTicket(patch: { status?: string; priority?: string }) {
  if (!current.value) return
  metaSaving.value = true
  try {
    await updateTicket(current.value.id, patch)
    Object.assign(current.value, patch)
    ElMessage.success('工单已更新并通知用户')
    load()
  } catch {
    // 拦截器已提示
  } finally {
    metaSaving.value = false
  }
}

const STATUS_TABS = [
  { value: '', label: '全部' },
  { value: 'open', label: '进行中' },
  { value: 'pending', label: '待处理' },
  { value: 'closed', label: '已关闭' },
]

const PRIORITY_LABELS: Record<string, string> = { low: '低', medium: '中', high: '高', urgent: '紧急' }

function fmtDate(s: string): string {
  return s.slice(0, 16).replace('T', ' ')
}

function statusLabel(status: string): string {
  const map: Record<string, string> = { open: '进行中', pending: '待处理', closed: '已关闭' }
  return map[status] || status
}

function statusBadge(status: string): string {
  const map: Record<string, string> = { open: 'au-badge-green', pending: 'au-badge-amber', closed: 'au-badge-muted' }
  return map[status] || 'au-badge-muted'
}
</script>

<template>
  <div class="admin-page">
    <PageHeader eyebrow="内容与服务" title="工单" description="点任意一行打开对话；回复会以站内消息通知用户，可在详情里直接改状态与优先级。">
      <template #actions>
        <el-button :loading="loading" @click="load" :icon="RefreshCw">刷新</el-button>
      </template>
    </PageHeader>

    <SectionCard title="工单列表" :icon="Ticket" :meta="loading ? '' : `${list.length} 条`" flush>
      <div class="list-bar">
        <div class="toolbar">
          <el-radio-group v-model="statusFilter" aria-label="按状态筛选" @change="load">
            <el-radio-button v-for="t in STATUS_TABS" :key="t.value" :value="t.value">{{ t.label }}</el-radio-button>
          </el-radio-group>
        </div>
      </div>

      <DataTable
        :rows="list"
        :columns="columns"
        :loading="loading"
        :error="loadError"
        :empty="statusFilter ? `没有${statusLabel(statusFilter)}的工单` : '还没有工单'"
        :empty-description="statusFilter ? '切到「全部」看看其它状态。' : '用户提交的工单会出现在这里。'"
        clickable
        @row-click="openDetail"
        @retry="load"
      >
        <template #cell-title="{ row }">
          <button type="button" class="ticket-title" @click="openDetail(row)">{{ row.title }}</button>
          <div class="ticket-preview">{{ row.latest_message || '—' }}</div>
        </template>

        <template #cell-user_name="{ row }">{{ row.user_name }}</template>

        <template #cell-category="{ row }">{{ row.category }}</template>

        <template #cell-priority="{ row }">
          <span class="prio" :class="row.priority">{{ PRIORITY_LABELS[row.priority] || row.priority }}</span>
        </template>

        <template #cell-status="{ row }">
          <span class="au-badge" :class="statusBadge(row.status)">{{ statusLabel(row.status) }}</span>
        </template>

        <template #cell-updated_at="{ row }"><span class="mono">{{ fmtDate(row.updated_at) }}</span></template>

        <template #cell-actions="{ row }">
          <!-- 整行可点开详情后，「关闭」必须拦住冒泡，否则点关闭会同时打开抽屉 -->
          <span @click.stop>
            <el-button v-if="row.status !== 'closed'" size="small" type="danger" :loading="rowBusyId === row.id" @click="close(row)">
              关闭
            </el-button>
            <span v-else class="muted">已关闭</span>
          </span>
        </template>
      </DataTable>
    </SectionCard>

    <el-drawer v-model="drawerVisible" :title="current?.title || '工单详情'" size="min(460px, 92vw)">
      <div v-if="current" class="ticket-meta">
        <dl class="meta-line">
          <div><dt>提交人</dt><dd>{{ current.user_name }}</dd></div>
          <div><dt>分类</dt><dd>{{ current.category }}</dd></div>
          <div><dt>创建</dt><dd class="mono">{{ fmtDate(current.created_at) }}</dd></div>
        </dl>
        <div class="meta-controls">
          <label class="meta-field">
            <span class="meta-label">状态</span>
            <el-select
              :model-value="current.status"
              size="small"
              class="w-meta"
              :disabled="metaSaving"
              @change="(v: string) => patchTicket({ status: v })"
            >
              <el-option label="进行中" value="open" />
              <el-option label="待处理" value="pending" />
              <el-option label="已关闭" value="closed" />
            </el-select>
          </label>
          <label class="meta-field">
            <span class="meta-label">优先级</span>
            <el-select
              :model-value="current.priority"
              size="small"
              class="w-meta"
              :disabled="metaSaving"
              @change="(v: string) => patchTicket({ priority: v })"
            >
              <el-option v-for="(label, key) in PRIORITY_LABELS" :key="key" :label="label" :value="key" />
            </el-select>
          </label>
        </div>
      </div>

      <div class="msg-list">
        <div v-if="detailLoading" class="msg-loading">
          <el-skeleton :rows="3" animated />
        </div>
        <EmptyState v-else-if="detailError" compact :icon="Inbox" title="对话读取失败">
          <template #actions>
            <el-button size="small" @click="current && openDetail(current)">重试</el-button>
          </template>
        </EmptyState>
        <EmptyState v-else-if="!messages.length" compact :icon="Inbox" title="还没有对话内容" />
        <div v-for="m in messages" :key="m.id" class="msg" :class="{ admin: m.is_admin }">
          <div class="msg-meta">
            {{ m.is_admin ? (m.admin_name || '管理员') : current?.user_name }} · {{ fmtDate(m.created_at) }}
          </div>
          <div class="msg-body">{{ m.message }}</div>
        </div>
      </div>

      <div v-if="current && current.status !== 'closed'" class="reply-box">
        <el-input v-model="replyText" type="textarea" :rows="3" placeholder="输入回复内容…" />
        <div class="reply-actions">
          <el-button :disabled="sending || !replyText.trim()" @click="send(false)" :icon="Send">
            回复
          </el-button>
          <el-button type="primary" :disabled="sending || !replyText.trim()" @click="send(true)">回复并关闭</el-button>
        </div>
      </div>
      <div v-else class="closed-hint">工单已关闭</div>
    </el-drawer>
  </div>
</template>

<style scoped>
.list-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px 12px;
  flex-wrap: wrap;
  padding: 4px 20px 12px;
}

.ticket-title {
  background: none;
  border: none;
  color: var(--au-text);
  font-weight: 600;
  font-size: 14px;
  cursor: pointer;
  padding: 0;
  text-align: left;
  border-radius: var(--au-r-sm);
}
.ticket-title:hover { color: var(--au-primary); }
.ticket-title:focus-visible { outline: 2px solid var(--au-border-focus); outline-offset: 2px; }

.ticket-preview {
  font-size: 12px;
  color: var(--au-text-3);
  margin-top: 2px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 320px;
}

.prio { font-size: 12px; color: var(--au-text-2); }
.prio.high, .prio.urgent { color: var(--au-danger); font-weight: 600; }
.prio.low { color: var(--au-text-4); }
.muted { color: var(--au-text-4); font-size: 12px; }

.ticket-meta {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 12px 14px;
  margin-bottom: 16px;
  border-radius: var(--au-r-md);
  border: 1px solid var(--au-border);
  background: var(--au-surface-2);
}

.meta-line {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 18px;
  margin: 0;
  font-size: 12.5px;
}
.meta-line div { display: flex; gap: 6px; }
.meta-line dt, .meta-label { color: var(--au-text-3); }
.meta-line dd { margin: 0; color: var(--au-text); }

.meta-controls { display: flex; align-items: center; gap: 10px 18px; flex-wrap: wrap; font-size: 12.5px; }
.meta-field { display: inline-flex; align-items: center; gap: 8px; }
.w-meta { width: 110px; }

.msg-list { display: flex; flex-direction: column; gap: 10px; }
.msg {
  max-width: 92%;
  align-self: flex-start;
  background: var(--au-surface-2);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-md);
  padding: 10px 12px;
}
.msg.admin { align-self: flex-end; background: var(--au-primary-soft); border-color: var(--au-primary-border); }
.msg-meta { font-size: 11.5px; color: var(--au-text-3); margin-bottom: 4px; }
.msg-body { font-size: 13px; line-height: 1.6; white-space: pre-wrap; color: var(--au-text); overflow-wrap: anywhere; }

.reply-box { margin-top: 16px; padding-top: 16px; border-top: 1px solid var(--au-border); }
.reply-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 10px; flex-wrap: wrap; }
.closed-hint { margin-top: 16px; text-align: center; color: var(--au-text-3); font-size: 13px; }

@media (max-width: 768px) {
  .list-bar { padding: 4px 16px 12px; }
  .list-bar :deep(.el-radio-group) { flex-wrap: nowrap; overflow-x: auto; max-width: 100%; }
}

@media (max-width: 640px) {
  .ticket-preview { max-width: 100%; }
  .msg { max-width: 100%; }
  .reply-actions :deep(.el-button) { flex: 1; margin-left: 0; }
}
</style>
