<script setup lang="ts">
/**
 * 公益服 · 活动提案审批（等级治理 Step 3 管理端入口）
 *
 * 用户端 Lv.2+ 提交的群抽奖活动提案在这里审批（后端 backend/api/welfare_admin.py
 * 的 /welfare/lottery/proposals 三口，逻辑在 backend/lottery_governance.py）：
 * - 通过：后端重校验提案内容、群白名单与同群进行中活动，然后按提案内容建一轮抽奖
 *   （发起人记为提案人），并向群里发新活动公告；之后走现有开奖机制
 *   （到点自动开奖 / 在「群抽奖」标签里手动开奖）。
 * - 驳回：只改提案状态，不建轮次、不发奖。
 * 两个动作都可附审批备注，都写管理端审计日志；已处理的提案不能再改。
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { CheckCircle2, ClipboardList, Gift, RefreshCw, XCircle } from 'lucide-vue-next'
import { EmptyState, PageHeader, SectionCard } from '@/components/ui'
import { useBreakpoint } from '@/composables/useBreakpoint'
import { useAuthStore } from '@/stores/auth'
import { toAdminError } from '@/utils/errorMessage'
import DataTable from '@/components/DataTable.vue'
import type { DataColumn } from '@/components/DataTable.vue'
import {
  approveLotteryProposal,
  fetchLotteryProposals,
  rejectLotteryProposal,
  type LotteryProposalRow,
  type LotteryProposalStatus,
} from '@/api/welfare'

const statusMap: Record<LotteryProposalStatus, string> = { pending: '待审批', approved: '已通过', rejected: '已驳回' }
const statusTagType = (s?: string): 'warning' | 'success' | 'info' =>
  (({ pending: 'warning', approved: 'success', rejected: 'info' } as Record<string, 'warning' | 'success' | 'info'>)[s ?? ''] ?? 'info')
const prizeTypeMap: Record<string, string> = { days: '公益天数', points: '积分', whitelist: '白名单' }
const lotteryTypeLabel = (t?: string) => (t === 'password' ? '口令抽奖' : '按钮抽奖')
const fmtTime = (v?: string | null) => (v ? String(v).replace('T', ' ').slice(0, 19) : '—')
const prizeSummary = (row: LotteryProposalRow) =>
  (row.prizes ?? []).map(p => `${p.name || prizeTypeMap[p.type] || p.type}×${p.quantity}`).join('、') || '—'

const NOTE_MAX = 500 // 后端 review_note 列 String(500)

const { isPhone } = useBreakpoint()
const auth = useAuthStore()
/** 只读审计角色不能写：按钮置灰，服务端仍会自己判一次 */
const canWrite = computed(() => auth.admin?.can_write !== false)

// ---------- 列表 ----------
const statusFilter = ref<LotteryProposalStatus | ''>('pending')
const loading = ref(false)
const loadError = ref('')
const rows = ref<LotteryProposalRow[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = 20

const columns = computed<DataColumn[]>(() => [
  { key: 'title', label: '活动标题', minWidth: 180, mobile: 'title' },
  { key: 'user_id', label: '提案人ID', width: 100 },
  { key: 'level_snapshot', label: '提交时等级', width: 100 },
  { key: 'chat_id', label: '群ID', width: 150 },
  { key: 'prizes', label: '奖品', minWidth: 180 },
  { key: 'status', label: '状态', width: 90 },
  { key: 'created_at', label: '提交时间', width: 170 },
  { key: 'actions', label: '操作', width: 180, fixed: 'right', align: 'right' },
])

const load = async () => {
  loading.value = true
  loadError.value = ''
  try {
    const res = await fetchLotteryProposals({
      page: page.value,
      page_size: pageSize,
      ...(statusFilter.value ? { status: statusFilter.value } : {}),
    })
    rows.value = res.items ?? []
    total.value = res.total ?? 0
  } catch (e) {
    loadError.value = toAdminError(e, '加载失败')
  } finally {
    loading.value = false
  }
}

const onFilterChange = () => {
  page.value = 1
  load()
}
const clearFilter = () => {
  statusFilter.value = ''
  onFilterChange()
}

// ---------- 详情 ----------
const detailVisible = ref(false)
const detail = ref<LotteryProposalRow | null>(null)
const openDetail = (row: LotteryProposalRow) => {
  detail.value = row
  detailVisible.value = true
}

// ---------- 审批（通过 / 驳回，弹窗即二次确认） ----------
type ReviewAction = 'approve' | 'reject'
const reviewVisible = ref(false)
const reviewAction = ref<ReviewAction>('approve')
const reviewTarget = ref<LotteryProposalRow | null>(null)
const reviewNote = ref('')
const reviewing = ref(false)

const openReview = (row: LotteryProposalRow, action: ReviewAction) => {
  reviewTarget.value = row
  reviewAction.value = action
  reviewNote.value = ''
  reviewVisible.value = true
}

const submitReview = async () => {
  const target = reviewTarget.value
  if (!target || reviewing.value) return
  const note = reviewNote.value.trim() || null
  reviewing.value = true
  try {
    if (reviewAction.value === 'approve') {
      const res = await approveLotteryProposal(target.id, note)
      const notified = res.notify?.sent === true
      ElMessage.success(
        `已通过，已建抽奖活动 #${res.round?.id ?? '?'}` +
          (notified ? '，群公告已发送' : '，群公告未发出（可到群里手动告知）') +
          '；在「群抽奖」标签刷新即可看到',
      )
      if (detail.value?.id === target.id) detail.value = res.proposal
    } else {
      const res = await rejectLotteryProposal(target.id, note)
      ElMessage.success('已驳回提案')
      if (detail.value?.id === target.id) detail.value = res
    }
    reviewVisible.value = false
    load()
  } catch {
    /* 写操作失败由请求拦截器统一弹错（如「提案已处理」「该群已有进行中的抽奖活动」） */
  } finally {
    reviewing.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="admin-page">
    <PageHeader
      eyebrow="内容"
      title="活动提案审批"
      description="审批用户（Lv.2 及以上）提交的群抽奖活动提案：通过后自动建活动并发群公告，驳回不产生任何活动"
    >
      <template #actions>
        <el-select
          v-model="statusFilter"
          class="status-filter"
          aria-label="按状态筛选"
          @change="onFilterChange"
        >
          <el-option label="全部状态" value="" />
          <el-option label="待审批" value="pending" />
          <el-option label="已通过" value="approved" />
          <el-option label="已驳回" value="rejected" />
        </el-select>
        <el-button :icon="RefreshCw" :loading="loading" @click="load">刷新</el-button>
      </template>
    </PageHeader>

    <SectionCard
      title="提案列表"
      :icon="ClipboardList"
      :meta="`共 ${total} 条${statusFilter ? `（${statusMap[statusFilter]}）` : ''}`"
      flush
    >
      <DataTable
        :columns="columns"
        :rows="rows"
        :loading="loading"
        :error="loadError"
        empty="暂无活动提案"
        empty-description="用户在用户端「等级」区提交活动提案后会出现在这里。"
        :has-active-filters="!!statusFilter"
        :pagination="{ total, page, pageSize }"
        @retry="load"
        @clear-filters="clearFilter"
        @page-change="(p: number) => { page = p; load() }"
      >
        <template #cell-level_snapshot="{ row }">Lv.{{ row.level_snapshot }}</template>
        <template #cell-prizes="{ row }">
          <span class="prize-summary">{{ prizeSummary(row) }}</span>
        </template>
        <template #cell-status="{ row }">
          <el-tag :type="statusTagType(row.status)" size="small">{{ statusMap[row.status as LotteryProposalStatus] ?? row.status }}</el-tag>
        </template>
        <template #cell-created_at="{ row }">{{ fmtTime(row.created_at) }}</template>
        <template #cell-actions="{ row }">
          <el-button link type="primary" @click="openDetail(row)">详情</el-button>
          <template v-if="row.status === 'pending'">
            <el-button link type="success" :disabled="!canWrite" @click="openReview(row, 'approve')">通过</el-button>
            <el-button link type="danger" :disabled="!canWrite" @click="openReview(row, 'reject')">驳回</el-button>
          </template>
        </template>
      </DataTable>
    </SectionCard>

    <!-- 详情 -->
    <el-drawer v-model="detailVisible" title="提案详情" :size="isPhone ? '100%' : '600px'">
      <template v-if="detail">
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item label="提案ID">#{{ detail.id }}</el-descriptions-item>
          <el-descriptions-item label="状态">
            <el-tag :type="statusTagType(detail.status)" size="small">{{ statusMap[detail.status] ?? detail.status }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="活动标题">{{ detail.title }}</el-descriptions-item>
          <el-descriptions-item label="提案人">用户 ID {{ detail.user_id }}（提交时 Lv.{{ detail.level_snapshot }}）</el-descriptions-item>
          <el-descriptions-item label="群ID">{{ detail.chat_id }}</el-descriptions-item>
          <el-descriptions-item label="抽奖类型">{{ lotteryTypeLabel(detail.lottery_type) }}</el-descriptions-item>
          <el-descriptions-item v-if="detail.lottery_type === 'password'" label="口令">{{ detail.password_keyword || '—' }}</el-descriptions-item>
          <el-descriptions-item label="开奖时间">{{ detail.draw_at ? fmtTime(detail.draw_at) : '未设（需手动开奖）' }}</el-descriptions-item>
          <el-descriptions-item label="人数上限">{{ detail.max_participants ? detail.max_participants : '不限' }}</el-descriptions-item>
          <el-descriptions-item label="提交时间">{{ fmtTime(detail.created_at) }}</el-descriptions-item>
          <template v-if="detail.status !== 'pending'">
            <el-descriptions-item label="审批人ID">{{ detail.reviewer_id ?? '—' }}</el-descriptions-item>
            <el-descriptions-item label="审批时间">{{ fmtTime(detail.reviewed_at) }}</el-descriptions-item>
            <el-descriptions-item label="审批备注">{{ detail.review_note || '—' }}</el-descriptions-item>
          </template>
          <el-descriptions-item v-if="detail.round_id" label="生成的活动">
            抽奖活动 #{{ detail.round_id }}（在「群抽奖」标签查看开奖与发放）
          </el-descriptions-item>
        </el-descriptions>

        <div class="detail-title"><Gift :size="14" /> 奖品</div>
        <el-table :data="detail.prizes ?? []" size="small" border>
          <el-table-column prop="name" label="名称" show-overflow-tooltip />
          <el-table-column label="类型" width="100">
            <template #default="{ row }">{{ prizeTypeMap[row.type] ?? row.type }}</template>
          </el-table-column>
          <el-table-column prop="value" label="值" width="90" />
          <el-table-column prop="quantity" label="份数" width="90" />
        </el-table>
        <EmptyState v-if="!(detail.prizes ?? []).length" compact title="提案未包含奖品" />
      </template>
      <template v-if="detail?.status === 'pending'" #footer>
        <el-button type="danger" plain :disabled="!canWrite" @click="openReview(detail, 'reject')">驳回</el-button>
        <el-button type="success" :disabled="!canWrite" @click="openReview(detail, 'approve')">通过</el-button>
      </template>
    </el-drawer>

    <!-- 审批确认 -->
    <el-dialog
      v-model="reviewVisible"
      :title="reviewAction === 'approve' ? '确认通过提案' : '确认驳回提案'"
      width="min(520px, 92vw)"
      append-to-body
    >
      <template v-if="reviewTarget">
        <p class="review-target">
          提案 #{{ reviewTarget.id }}「{{ reviewTarget.title }}」· 群 {{ reviewTarget.chat_id }} · 奖品 {{ prizeSummary(reviewTarget) }}
        </p>
        <el-alert
          v-if="reviewAction === 'approve'"
          type="warning"
          :closable="false"
          show-icon
          title="通过后会立即生效，不可撤回"
        >
          <ul class="review-effects">
            <li>按提案内容在该群新建一轮抽奖（发起人记为提案人），并向群里发送新活动公告。</li>
            <li>之后走正常开奖流程：设了开奖时间则到点自动开奖，否则需在「群抽奖」标签手动开奖；奖品按现有口径发放。</li>
            <li>若该群已有进行中的活动、群不在允许列表、抽奖功能已关闭或非公益服，通过会失败，提案保持待审批。</li>
            <li>如需撤销，只能到「群抽奖」标签取消生成的活动。</li>
          </ul>
        </el-alert>
        <el-alert
          v-else
          type="info"
          :closable="false"
          show-icon
          title="驳回只改提案状态，不建活动、不发奖，处理后不可再改"
        />
        <el-form label-position="top" class="review-form" @submit.prevent>
          <el-form-item :label="reviewAction === 'approve' ? '审批备注（可选）' : '驳回理由（可选）'">
            <el-input
              v-model="reviewNote"
              type="textarea"
              :rows="3"
              :maxlength="NOTE_MAX"
              show-word-limit
              :placeholder="reviewAction === 'approve' ? '记录在提案上，供审计查看' : '例如：奖品设置过高 / 与近期活动重复'"
            />
          </el-form-item>
        </el-form>
      </template>
      <template #footer>
        <el-button @click="reviewVisible = false">取消</el-button>
        <el-button
          :type="reviewAction === 'approve' ? 'success' : 'danger'"
          :icon="reviewAction === 'approve' ? CheckCircle2 : XCircle"
          :loading="reviewing"
          :disabled="!canWrite"
          @click="submitReview"
        >
          {{ reviewAction === 'approve' ? '确认通过' : '确认驳回' }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.status-filter { width: 130px; }
.prize-summary { color: var(--au-text-2); font-size: 13px; }
.detail-title { display: flex; align-items: center; gap: 6px; margin: 16px 0 8px; font-weight: 600; color: var(--au-text); }
.detail-title svg { color: var(--au-primary); }
.review-target { margin: 0 0 12px; color: var(--au-text); font-size: 14px; line-height: 1.6; word-break: break-all; }
.review-effects { margin: 4px 0 0; padding-left: 18px; line-height: 1.7; font-size: 13px; }
.review-form { margin-top: 14px; }
</style>
