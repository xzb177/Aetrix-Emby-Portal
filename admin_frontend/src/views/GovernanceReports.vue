<script setup lang="ts">
/**
 * 举报终审（治理 Step 2）：复核达标升级上来的举报由管理员受理 / 驳回
 *
 * - 默认只看终审队列（escalated，最早在前）；可切到其它状态查历史；
 * - 点一行打开详情抽屉（含全部复核票）；
 * - 判定：在抽屉里选受理 / 驳回 + 备注，再弹一次二次确认才提交；
 *   受理不会自动隐藏 / 删除求片本身（v1 需人工到「求片管理」处理）；
 *   驳回计入举报人的终审驳回计数，达标时后端自动停权，结果随响应带回并提示。
 *
 * 只读审计（viewer）角色：判定按钮置灰，服务端仍会自己判一次。
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Flag, Inbox, RefreshCw } from 'lucide-vue-next'
import { EmptyState, PageHeader, SectionCard } from '@/components/ui'
import { parseServerTime } from '@/utils/serverTime'
import DataTable from '@/components/DataTable.vue'
import type { DataColumn } from '@/components/DataTable.vue'
import { useAuthStore } from '@/stores/auth'
import { useQueryFilter } from '@/composables/useQueryFilter'
import {
  decideGovernanceReport,
  fetchGovernanceReport,
  fetchGovernanceReports,
} from '@/api/governance'
import type {
  GovernanceDecision,
  GovernanceReportDetail,
  GovernanceReportRow,
} from '@/api/governance'

const auth = useAuthStore()
/** 只读角色不能写：界面置灰，服务端仍会自己判一次 */
const canWrite = computed(() => auth.admin?.can_write !== false)

const columns: DataColumn[] = [
  { key: 'id', label: '举报', minWidth: 200, mobile: 'title' },
  { key: 'reporter_id', label: '举报人', width: 110 },
  { key: 'reason', label: '原因', width: 110 },
  { key: 'votes', label: '复核（同意 / 不同意）', width: 170 },
  { key: 'status', label: '状态', width: 110 },
  { key: 'time', label: '时间', width: 150, mobile: 'hide' },
  { key: 'actions', label: '操作', width: 100, fixed: 'right', align: 'right' },
]

/** 状态筛选：'' = 终审队列（后端默认 escalated） */
const STATUS_TABS = [
  { value: '', label: '终审队列' },
  { value: 'pending', label: '待复核' },
  { value: 'accepted', label: '已受理' },
  { value: 'rejected', label: '已驳回' },
  { value: 'review_rejected', label: '复核关闭' },
]

const STATUS_LABELS: Record<string, string> = {
  pending: '待复核',
  escalated: '待终审',
  accepted: '已受理',
  rejected: '已驳回',
  review_rejected: '复核关闭',
}
const STATUS_BADGES: Record<string, string> = {
  pending: 'au-badge-info',
  escalated: 'au-badge-amber',
  accepted: 'au-badge-green',
  rejected: 'au-badge-rose',
  review_rejected: 'au-badge-muted',
}
const REASON_LABELS: Record<string, string> = {
  spam: '刷屏 / 广告',
  duplicate: '重复求片',
  violation: '违规内容',
}
const TARGET_LABELS: Record<string, string> = { media_seek: '求片' }

const list = ref<GovernanceReportRow[]>([])
const loading = ref(false)
const loadError = ref('')
const statusFilter = ref('')
// 深链：/governance?status=accepted 直接落到对应筛选
useQueryFilter(statusFilter, 'status', load)

const drawerVisible = ref(false)
const current = ref<GovernanceReportRow | null>(null)
const detail = ref<GovernanceReportDetail | null>(null)
const detailLoading = ref(false)
const detailError = ref(false)

const decision = ref<GovernanceDecision>('accepted')
const note = ref('')
const deciding = ref(false)

const sectionTitle = computed(() =>
  statusFilter.value ? `${statusLabel(statusFilter.value)}的举报` : '终审队列',
)

function statusLabel(s: string): string {
  return STATUS_LABELS[s] || s
}
function statusBadge(s: string): string {
  return STATUS_BADGES[s] || 'au-badge-muted'
}
function reasonLabel(r: string): string {
  return REASON_LABELS[r] || r
}
function targetLabel(r: GovernanceReportRow): string {
  return `${TARGET_LABELS[r.target_type] || r.target_type} #${r.target_id}`
}

function fmtTime(v: string | null | undefined): string {
  if (!v) return '—'
  const d = parseServerTime(v)
  if (Number.isNaN(d.getTime())) return '—'
  const p2 = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())} ${p2(d.getHours())}:${p2(d.getMinutes())}`
}

/** 列表时间列：终审队列看升级时间，已判的看判定时间，其它看提交时间 */
function rowTime(r: GovernanceReportRow): string {
  if (r.decided_at) return fmtTime(r.decided_at)
  if (r.escalated_at) return fmtTime(r.escalated_at)
  return fmtTime(r.created_at)
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    list.value = await fetchGovernanceReports(
      statusFilter.value ? { status: statusFilter.value, limit: 200 } : { limit: 200 },
    )
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : '加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(load)

async function openDetail(r: GovernanceReportRow) {
  current.value = r
  detail.value = null
  decision.value = 'accepted'
  note.value = ''
  drawerVisible.value = true
  detailLoading.value = true
  detailError.value = false
  try {
    detail.value = await fetchGovernanceReport(r.id)
  } catch {
    detailError.value = true
  } finally {
    detailLoading.value = false
  }
}

/** 抽屉里展示的数据：详情到了用详情，没到先用列表行 */
const shown = computed<GovernanceReportRow | null>(() => detail.value || current.value)
const canDecide = computed(() => shown.value?.status === 'escalated')

async function submitDecision() {
  const r = shown.value
  if (!r || !canDecide.value || deciding.value) return
  const accept = decision.value === 'accepted'
  const message = accept
    ? `确定受理举报 #${r.id}（${targetLabel(r)}）吗？受理只记录终审结论，不会自动隐藏或删除该求片，需要到「求片管理」另行处理。`
    : `确定驳回举报 #${r.id} 吗？驳回会计入举报人（用户 #${r.reporter_id}）的终审驳回次数，达到阈值时系统会自动暂停其治理权限。`
  try {
    await ElMessageBox.confirm(message, accept ? '确认受理' : '确认驳回', {
      confirmButtonText: accept ? '受理' : '驳回',
      cancelButtonText: '取消',
      type: 'warning',
      confirmButtonClass: accept ? '' : 'el-button--danger',
    })
  } catch {
    return // 用户点了取消
  }
  deciding.value = true
  try {
    const noteText = note.value.trim()
    const res = await decideGovernanceReport(r.id, {
      decision: decision.value,
      ...(noteText ? { note: noteText } : {}),
    })
    ElMessage.success(accept ? '已受理' : '已驳回')
    if (res.auto_suspension) {
      ElMessageBox.alert(
        `举报人（用户 #${res.auto_suspension.user_id}）终审驳回次数已达阈值，系统已自动暂停其治理权限至 ${fmtTime(res.auto_suspension.ends_at)}。可在「治理停权」标签查看或撤销。`,
        '已触发自动停权',
        { type: 'info', confirmButtonText: '知道了' },
      ).catch(() => {})
    }
    detail.value = detail.value ? { ...detail.value, ...res } : null
    current.value = { ...r, ...res }
    load()
  } catch {
    // 拦截器已提示（例如 409：该举报已不在终审队列）；刷新列表与详情避免停留在过期状态
    load()
    if (current.value) openDetail(current.value)
  } finally {
    deciding.value = false
  }
}
</script>

<template>
  <div>
    <PageHeader
      eyebrow="内容"
      title="举报终审"
      description="复核员投票达标后举报进入终审队列，由管理员受理或驳回；受理不会自动处理求片本身，驳回会计入举报人的恶意举报计数。"
    >
      <template #actions>
        <el-button :loading="loading" :icon="RefreshCw" @click="load">刷新</el-button>
      </template>
    </PageHeader>

    <SectionCard :title="sectionTitle" :icon="Flag" :meta="loading ? '' : `${list.length} 条`" flush>
      <div class="list-bar">
        <el-radio-group v-model="statusFilter" aria-label="按状态筛选" @change="load">
          <el-radio-button v-for="t in STATUS_TABS" :key="t.value" :value="t.value">{{ t.label }}</el-radio-button>
        </el-radio-group>
        <span v-if="!canWrite" class="muted">当前为只读角色：可以查看，不能判定</span>
      </div>

      <DataTable
        :rows="list"
        :columns="columns"
        :loading="loading"
        :error="loadError"
        :empty="statusFilter ? `没有${statusLabel(statusFilter)}的举报` : '终审队列是空的'"
        :empty-description="statusFilter ? '切到其它状态看看。' : '复核达标的举报会出现在这里。'"
        clickable
        @row-click="openDetail"
        @retry="load"
      >
        <template #cell-id="{ row }">
          <button type="button" class="row-title" @click.stop="openDetail(row)">
            #{{ row.id }} · {{ targetLabel(row) }}
          </button>
          <div class="row-preview">{{ row.detail || '（无补充说明）' }}</div>
        </template>
        <template #cell-reporter_id="{ row }"><span class="mono">用户 #{{ row.reporter_id }}</span></template>
        <template #cell-reason="{ row }">{{ reasonLabel(row.reason) }}</template>
        <template #cell-votes="{ row }">
          <span class="mono">{{ row.agree_weight }} / {{ row.disagree_weight }}</span>
          <span class="muted"> · {{ row.ballots }} 票</span>
        </template>
        <template #cell-status="{ row }">
          <span class="au-badge" :class="statusBadge(row.status)">{{ statusLabel(row.status) }}</span>
        </template>
        <template #cell-time="{ row }"><span class="mono">{{ rowTime(row) }}</span></template>
        <template #cell-actions="{ row }">
          <span @click.stop>
            <el-button size="small" :type="row.status === 'escalated' ? 'primary' : 'default'" @click="openDetail(row)">
              {{ row.status === 'escalated' && canWrite ? '判定' : '查看' }}
            </el-button>
          </span>
        </template>
      </DataTable>
    </SectionCard>

    <el-drawer
      v-model="drawerVisible"
      :title="shown ? `举报 #${shown.id}` : '举报详情'"
      size="min(480px, 92vw)"
    >
      <template v-if="shown">
        <div class="meta-box">
          <dl class="meta-grid">
            <div><dt>举报对象</dt><dd>{{ targetLabel(shown) }}</dd></div>
            <div><dt>举报人</dt><dd class="mono">用户 #{{ shown.reporter_id }}</dd></div>
            <div><dt>原因</dt><dd>{{ reasonLabel(shown.reason) }}</dd></div>
            <div>
              <dt>状态</dt>
              <dd><span class="au-badge" :class="statusBadge(shown.status)">{{ statusLabel(shown.status) }}</span></dd>
            </div>
            <div><dt>提交时间</dt><dd class="mono">{{ fmtTime(shown.created_at) }}</dd></div>
            <div><dt>升级终审</dt><dd class="mono">{{ fmtTime(shown.escalated_at) }}</dd></div>
            <div v-if="shown.decided_at"><dt>判定时间</dt><dd class="mono">{{ fmtTime(shown.decided_at) }}</dd></div>
            <div v-if="shown.admin_id"><dt>判定管理员</dt><dd class="mono">#{{ shown.admin_id }}</dd></div>
          </dl>
          <div class="meta-block">
            <div class="meta-label">补充说明</div>
            <div class="meta-text">{{ shown.detail || '（无）' }}</div>
          </div>
          <div v-if="shown.admin_note" class="meta-block">
            <div class="meta-label">终审备注</div>
            <div class="meta-text">{{ shown.admin_note }}</div>
          </div>
        </div>

        <h4 class="section-h">
          复核票
          <span class="muted">同意 {{ shown.agree_weight }} · 不同意 {{ shown.disagree_weight }}（按权重）· 共 {{ shown.ballots }} 人</span>
        </h4>
        <div v-if="detailLoading"><el-skeleton :rows="2" animated /></div>
        <EmptyState v-else-if="detailError" compact :icon="Inbox" title="详情读取失败">
          <template #actions>
            <el-button size="small" @click="current && openDetail(current)">重试</el-button>
          </template>
        </EmptyState>
        <EmptyState v-else-if="!detail?.votes?.length" compact :icon="Inbox" title="还没有复核票" />
        <ul v-else class="vote-list">
          <li v-for="v in detail.votes" :key="v.id" class="vote">
            <span class="mono">用户 #{{ v.reviewer_id }}</span>
            <span class="au-badge" :class="v.decision === 'agree' ? 'au-badge-green' : 'au-badge-rose'">
              {{ v.decision === 'agree' ? '同意' : '不同意' }}
            </span>
            <span class="muted">权重 {{ v.weight }}</span>
            <span class="muted mono vote-time">{{ fmtTime(v.created_at) }}</span>
          </li>
        </ul>

        <div v-if="canDecide" class="decide-box">
          <h4 class="section-h">终审判定</h4>
          <el-radio-group v-model="decision" :disabled="!canWrite || deciding" aria-label="终审决定">
            <el-radio value="accepted">受理（举报成立）</el-radio>
            <el-radio value="rejected">驳回（举报不成立）</el-radio>
          </el-radio-group>
          <p class="hint">
            {{ decision === 'accepted'
              ? '受理只记录结论，不会自动隐藏或删除求片，需要到「求片管理」另行处理。'
              : '驳回会计入举报人的终审驳回次数，达到阈值时自动暂停其治理权限。' }}
          </p>
          <el-input
            v-model="note"
            type="textarea"
            :rows="3"
            maxlength="500"
            show-word-limit
            placeholder="终审备注（可选，举报人可见）"
            :disabled="!canWrite || deciding"
            aria-label="终审备注"
          />
          <div class="decide-actions">
            <el-button
              :type="decision === 'accepted' ? 'primary' : 'danger'"
              :loading="deciding"
              :disabled="!canWrite"
              @click="submitDecision"
            >
              {{ decision === 'accepted' ? '受理…' : '驳回…' }}
            </el-button>
          </div>
          <p v-if="!canWrite" class="hint">当前为只读角色，不能判定。</p>
        </div>
        <div v-else class="closed-hint">
          {{ shown.status === 'pending' ? '该举报仍在复核中，复核达标后才能终审。' : '该举报已结束，不能再判定。' }}
        </div>
      </template>
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
.row-title {
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
.row-title:hover { color: var(--au-primary); }
.row-title:focus-visible { outline: 2px solid var(--au-border-focus); outline-offset: 2px; }
.row-preview {
  font-size: 12px;
  color: var(--au-text-3);
  margin-top: 2px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 320px;
}
.muted { color: var(--au-text-4); font-size: 12px; }

.meta-box {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 12px 14px;
  margin-bottom: 16px;
  border-radius: var(--au-r-md);
  border: 1px solid var(--au-border);
  background: var(--au-surface-2);
}
.meta-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px 18px;
  margin: 0;
  font-size: 12.5px;
}
.meta-grid div { display: flex; gap: 6px; align-items: center; min-width: 0; }
.meta-grid dt, .meta-label { color: var(--au-text-3); flex-shrink: 0; }
.meta-grid dd { margin: 0; color: var(--au-text); overflow-wrap: anywhere; }
.meta-label { font-size: 12px; margin-bottom: 4px; }
.meta-text { font-size: 13px; line-height: 1.6; white-space: pre-wrap; color: var(--au-text); overflow-wrap: anywhere; }

.section-h {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
  margin: 0 0 10px;
  font-size: 14px;
  font-weight: 600;
  color: var(--au-text);
}
.vote-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.vote {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding: 8px 12px;
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-md);
  font-size: 13px;
}
.vote-time { margin-left: auto; }

.decide-box { margin-top: 18px; padding-top: 16px; border-top: 1px solid var(--au-border); }
.decide-box :deep(.el-radio-group) { display: flex; flex-wrap: wrap; gap: 4px 16px; }
.hint { font-size: 12px; color: var(--au-text-3); margin: 8px 0; line-height: 1.6; }
.decide-actions { display: flex; justify-content: flex-end; margin-top: 10px; }
.closed-hint { margin-top: 18px; text-align: center; color: var(--au-text-3); font-size: 13px; }

@media (max-width: 768px) {
  .list-bar { padding: 4px 16px 12px; }
  .list-bar :deep(.el-radio-group) { flex-wrap: nowrap; overflow-x: auto; max-width: 100%; }
}
@media (max-width: 640px) {
  .row-preview { max-width: 100%; }
  .meta-grid { grid-template-columns: minmax(0, 1fr); }
  .decide-actions :deep(.el-button) { flex: 1; }
}
</style>
