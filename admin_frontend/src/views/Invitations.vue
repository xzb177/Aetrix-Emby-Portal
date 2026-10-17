<script setup lang="ts">
/**
 * 邀请与积分管理：邀请记录、积分流水、手动调整
 *
 * 参数配置（签到 / 支付 / 返利比例）已统一收归「系统设置」页，
 * 本页只负责台账与人工干预，避免两处入口改同一份配置。
 *
 * v2.6.11：两张台账改用 DataTable（手机上变成卡片列表，不再需要横向拖），
 * 统计瓦片统一为全局 .stat-tile。
 *
 * v2.54（暗房影院）：PageHeader + StatTile + SectionCard 原语；四张台账都是 flush 表格，
 * 邀请码的筛选在左、「批量生成」在右；积分流水分页收进卡片底栏；
 * 推广奖励的配置条改成发丝线分隔的表单行；三处台账加载失败都有可重试的错误态。
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import {
  AlertTriangle, Coins, Gift, HandCoins, History, Megaphone, Plus, QrCode, RefreshCw, Search, ShieldCheck, UserPlus,
} from 'lucide-vue-next'
import { EmptyState, PageHeader, SectionCard, StatTile } from '@/components/ui'
import {
  fetchInvitations,
  fetchPointsLogs,
  fetchEconomyStats,
  fetchEconomySettings,
  updateEconomySettings,
  verifyPointsAudit,
  adjustUserPoints,
  type InvitationRow,
  type PointsLogRow,
  type EconomyStats,
  type PointsAuditResult,
} from '@/api/economy'
import {
  fetchInvitationCodes,
  fetchPromotion,
  fetchUsers,
  generateInvitationCodes,
  revokeInvitationCodes,
  updateInvitationCode,
  type InvitationCodeRow,
  type PromotionResponse,
} from '@/api/admin'
import DataTable from '@/components/DataTable.vue'
import type { DataColumn } from '@/components/DataTable.vue'
import { useTableSelection } from '@/composables/useTableSelection'
import type { RowKey } from '@/composables/useTableSelection'

const loading = ref(false)
/** 积分流水单独的 loading：翻页 / 切类型只刷流水，不让邀请记录表跟着闪 */
const logsLoading = ref(false)
/** 积分流水加载失败（邀请记录 / 总览各自兜底，不影响主表） */
const logsError = ref(false)
/** 邀请记录读取失败：给出可重试的错误态，而不是假装「暂无邀请记录」 */
const invError = ref(false)
const stats = ref<EconomyStats | null>(null)

// ===== 邀请记录 =====
const invitations = ref<InvitationRow[]>([])

const inviteColumns: DataColumn[] = [
  { key: 'inviter', label: '邀请人', width: 140, mobile: 'title' },
  { key: 'invitee', label: '被邀请人', width: 140 },
  { key: 'reward_points', label: '奖励', width: 100 },
  { key: 'created_at', label: '时间' },
]

// ===== 积分流水 =====
const logs = ref<PointsLogRow[]>([])
const logTotal = ref(0)
const logPage = ref(1)
const logTypeFilter = ref('')

const logColumns: DataColumn[] = [
  { key: 'username', label: '用户', width: 130, mobile: 'title' },
  { key: 'amount', label: '变动', width: 90 },
  { key: 'type', label: '类型', width: 100 },
  { key: 'description', label: '说明', minWidth: 170 },
  { key: 'balance_after', label: '余额', width: 90 },
  { key: 'audited', label: '审计', width: 80 },
  { key: 'created_at', label: '时间', width: 160 },
]

// ===== C3 流水审计 =====
/** hash 链总开关（字符串 'true'/'false'，与经济设置其它布尔项一致） */
const auditEnabled = ref('true')
const auditSaving = ref(false)
const verifyUserId = ref<number | undefined>(undefined)
const verifyLoading = ref(false)
const verifyResult = ref<PointsAuditResult | null>(null)

async function loadAuditSettings() {
  try {
    const res = await fetchEconomySettings()
    auditEnabled.value = res.settings.points_audit_enabled ?? 'true'
  } catch {
    /* 读不到就保持默认开，拦截器已提示 */
  }
}

async function toggleAudit(v: boolean) {
  auditSaving.value = true
  try {
    await updateEconomySettings({ points_audit_enabled: v ? 'true' : 'false' })
    auditEnabled.value = v ? 'true' : 'false'
    ElMessage.success(v ? '流水审计已开启，新流水将写入 hash 链' : '流水审计已关闭，新流水不再写 hash')
  } finally {
    auditSaving.value = false
  }
}

async function handleVerify() {
  if (!verifyUserId.value) {
    ElMessage.warning('请先选择要核验的用户')
    return
  }
  verifyLoading.value = true
  verifyResult.value = null
  try {
    verifyResult.value = await verifyPointsAudit(verifyUserId.value)
  } finally {
    verifyLoading.value = false
  }
}

// ===== 手动调整 =====
const adjustVisible = ref(false)
const adjustFormRef = ref<FormInstance>()
const adjustForm = ref({ user_id: undefined as number | undefined, amount: 100, reason: '' })
const adjustRules: FormRules = {
  user_id: [{ required: true, message: '请选择用户', trigger: 'change' }],
  amount: [
    { required: true, message: '请输入调整数量', trigger: 'blur' },
    { type: 'number', message: '请输入有效的数字', trigger: 'blur' },
  ],
  reason: [{ max: 100, message: '原因最多 100 个字', trigger: 'blur' }],
}
const adjustLoading = ref(false)

function scrollToFirstError() {
  requestAnimationFrame(() => {
    document.querySelector('.el-dialog .el-form-item.is-error')?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  })
}

/** 用户搜索（远程）：不再要求管理员手填数据库 ID */
const userOptions = ref<{ id: number; username: string }[]>([])
const userSearching = ref(false)

async function searchUsers(keyword: string) {
  userSearching.value = true
  try {
    const res = await fetchUsers({ search: keyword || undefined, limit: 20 })
    userOptions.value = res.users.map((u) => ({ id: u.id, username: u.username }))
  } catch {
    userOptions.value = []
  } finally {
    userSearching.value = false
  }
}

const selectedUser = computed(() =>
  userOptions.value.find((u) => u.id === adjustForm.value.user_id)?.username,
)

const inviteTotal = computed(() => invitations.value.length)
const rebateTotal = computed(() =>
  logs.value.filter((l) => l.type === 'rebate').reduce((s, l) => s + l.amount, 0),
)

/** 邀请记录 + 经济总览：与流水的翻页 / 类型筛选无关，不随翻页重复请求 */
async function loadOverview() {
  loading.value = true
  invError.value = false
  try {
    const [inv, econ] = await Promise.all([
      fetchInvitations({ limit: 100 }).catch(() => null),
      fetchEconomyStats().catch(() => null),
    ])
    if (inv) invitations.value = inv.records
    else invError.value = true
    if (econ) stats.value = econ
  } finally {
    loading.value = false
  }
}

/** 请求序号：连续翻页 / 切类型时只采纳最后一次请求 */
let logsSeq = 0

/** 积分流水：翻页、切类型只拉这一个接口 */
async function loadLogs() {
  const seq = ++logsSeq
  logsLoading.value = true
  logsError.value = false
  try {
    const log = await fetchPointsLogs({
      limit: 30,
      offset: (logPage.value - 1) * 30,
      type_filter: logTypeFilter.value || undefined,
    })
    if (seq !== logsSeq) return
    logs.value = log.logs
    logTotal.value = log.total
  } catch {
    if (seq !== logsSeq) return
    // 错误提示由 HTTP 拦截器统一处理；这里只记下失败，给出重试入口
    logsError.value = true
  } finally {
    if (seq === logsSeq) logsLoading.value = false
  }
}

async function load() {
  await Promise.all([loadOverview(), loadLogs()])
}

async function openAdjust() {
  adjustForm.value = { user_id: undefined, amount: 100, reason: '' }
  adjustVisible.value = true
  await searchUsers('')
}

async function handleAdjust() {
  try {
    await adjustFormRef.value?.validate()
  } catch {
    scrollToFirstError()
    return
  }
  adjustLoading.value = true
  try {
    const res = await adjustUserPoints(adjustForm.value.user_id!, {
      amount: adjustForm.value.amount,
      reason: adjustForm.value.reason || undefined,
    })
    ElMessage.success(`调整成功，该用户当前余额 ${res.balance}`)
    adjustVisible.value = false
    load()
  } catch {
    // 错误提示由 HTTP 拦截器统一处理
  } finally {
    adjustLoading.value = false
  }
}

function fmtTime(iso?: string | null) {
  return iso ? iso.slice(0, 19).replace('T', ' ') : '—'
}

// ===== 邀请码管理（v2.44.0） =====
const invLoading = ref(false)
const invCodes = ref<InvitationCodeRow[]>([])
const invSummary = ref<Record<string, number>>({})
const invStates = ref<{ value: string; label: string }[]>([])
const invKeyword = ref('')
const codesError = ref(false)
const invState = ref('')

const invColumns: DataColumn[] = [
  { key: 'code', label: '邀请码', width: 130, mobile: 'title' },
  { key: 'owner_username', label: '归属', width: 120 },
  { key: 'use_count', label: '使用情况', width: 120 },
  { key: 'reward_points', label: '奖励', width: 100 },
  { key: 'state_label', label: '状态', width: 90 },
  { key: 'expires_at', label: '有效期', width: 150 },
  { key: 'whitelist', label: '白名单', minWidth: 150 },
  // 用 actions：DataTable 会把这一列渲染到手机卡片的底部操作区，
  // 用 action 会退化成一行「标签 / 值」，按钮变得不好按
  { key: 'actions', label: '操作', width: 150 },
]

async function loadCodes() {
  invLoading.value = true
  codesError.value = false
  try {
    const res = await fetchInvitationCodes({
      keyword: invKeyword.value || undefined,
      state: invState.value || undefined,
      limit: 300,
    })
    invCodes.value = res.codes
    invSummary.value = res.summary
    invStates.value = res.states
  } catch {
    /* 拦截器已提示 */
    codesError.value = true
  } finally {
    invLoading.value = false
  }
}

const usesText = (row: InvitationCodeRow) =>
  row.max_uses ? `${row.use_count} / ${row.max_uses}` : `${row.use_count} / 不限`

/**
 * 邀请码表批量作废（v2.58）
 * - 只动邀请码表这张 DataTable，邀请记录/积分流水两张表不动
 * - 复用后端真正的批量接口 revokeInvitationCodes(ids)，一次调用，不循环
 * - isRowSelectable：已作废（is_active=false）的行不可选
 */
const selection = useTableSelection()
/** 模板 v-model 用的可写代理：嵌套 ref 在模板里不会自动解包 */
const selectedKeys = computed<RowKey[]>({
  get: () => selection.selectedKeys.value,
  set: (v) => { selection.selectedKeys.value = v },
})
const bulkRevokeBusy = ref(false)

function onCodesFilter() {
  // 筛选变化清空选择：老选择对应的行可能已不在结果里。
  // 不要在 loadCodes 内部清——批量作废后也要调 loadCodes()，在那里清会误清失败项
  selection.clear()
  loadCodes()
}

/** v2.59 空状态"清除筛选"：清空邀请码筛选条件 */
function clearCodesFilters() {
  invKeyword.value = ''
  invState.value = ''
  onCodesFilter()
}

async function bulkRevokeCodes() {
  const ids = selection.selectedKeys.value as number[]
  if (!ids.length) return
  try {
    await ElMessageBox.confirm(
      `将作废 ${ids.length} 个邀请码？它们不能再被使用，但已产生的邀请关系不受影响。`,
      '批量作废邀请码',
      { type: 'warning', confirmButtonText: '作废', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  bulkRevokeBusy.value = true
  try {
    const res = await revokeInvitationCodes(ids)
    // 作废后行变不可选（is_active=false），留着选择无意义，全清
    selection.clear()
    ElMessage.success(res.message || `已作废 ${ids.length} 个邀请码`)
    await loadCodes()
  } catch {
    // 失败时不清空选择，保留可重试；错误由请求拦截器统一提示
  } finally {
    bulkRevokeBusy.value = false
  }
}

// --- 批量生成 ---
const genVisible = ref(false)
const genLoading = ref(false)
const genFormRef = ref<FormInstance>()
const genForm = ref({ owner_user_id: null as number | null, count: 5, max_uses: 100, expires_days: 0, whitelist: '', reward_points: 0 })
const genRules: FormRules = {
  owner_user_id: [{ required: true, message: '请选择归属用户', trigger: 'change' }],
  count: [
    { required: true, message: '请输入生成数量', trigger: 'blur' },
    { type: 'number', min: 1, max: 200, message: '数量需在 1–200 之间', trigger: 'blur' },
  ],
  max_uses: [{ type: 'number', min: 0, max: 100000, message: '次数需在 0–100000 之间', trigger: 'blur' }],
  expires_days: [{ type: 'number', min: 0, max: 3650, message: '天数需在 0–3650 之间', trigger: 'blur' }],
  reward_points: [{ type: 'number', min: 0, max: 1000000, message: '奖励需在 0–1000000 之间', trigger: 'blur' }],
}
const genOwner = computed(() =>
  userOptions.value.find((u) => u.id === genForm.value.owner_user_id)?.username,
)

async function openGen() {
  genForm.value = { owner_user_id: null, count: 5, max_uses: 100, expires_days: 0, whitelist: '', reward_points: 0 }
  genVisible.value = true
  if (!userOptions.value.length) await searchUsers('')
}

async function handleGen() {
  try {
    await genFormRef.value?.validate()
  } catch {
    scrollToFirstError()
    return
  }
  genLoading.value = true
  try {
    const res = await generateInvitationCodes({
      owner_user_id: genForm.value.owner_user_id!,
      count: genForm.value.count,
      max_uses: genForm.value.max_uses,
      expires_days: genForm.value.expires_days,
      whitelist: genForm.value.whitelist,
      reward_points: genForm.value.reward_points,
    })
    ElMessage.success(res.message)
    genVisible.value = false
    await loadCodes()
  } catch {
    /* 拦截器已提示 */
  } finally {
    genLoading.value = false
  }
}

// --- 改单张 ---
const editVisible = ref(false)
const editLoading = ref(false)
const editFormRef = ref<FormInstance>()
const editForm = ref({ id: 0, code: '', max_uses: 100, expires_days: 0, whitelist: '', is_active: true, reward_points: 0 })
const editRules: FormRules = {
  max_uses: [{ type: 'number', min: 0, max: 100000, message: '次数需在 0–100000 之间', trigger: 'blur' }],
  expires_days: [{ type: 'number', min: 0, max: 3650, message: '天数需在 0–3650 之间', trigger: 'blur' }],
  reward_points: [{ type: 'number', min: 0, max: 1000000, message: '奖励需在 0–1000000 之间', trigger: 'blur' }],
}

function openEdit(row: InvitationCodeRow) {
  editForm.value = {
    id: row.id,
    code: row.code,
    max_uses: row.max_uses,
    // 有效天数按「从现在起」重算；0 = 永不过期
    expires_days: row.expires_days_left ?? 0,
    whitelist: row.whitelist.join(','),
    is_active: row.is_active,
    reward_points: row.reward_points ?? 0,
  }
  editVisible.value = true
}

async function handleEdit() {
  try {
    await editFormRef.value?.validate()
  } catch {
    scrollToFirstError()
    return
  }
  editLoading.value = true
  try {
    const res = await updateInvitationCode(editForm.value.id, {
      max_uses: editForm.value.max_uses,
      expires_days: editForm.value.expires_days,
      whitelist: editForm.value.whitelist,
      is_active: editForm.value.is_active,
      reward_points: editForm.value.reward_points,
    })
    ElMessage.success('邀请码已更新')
    editVisible.value = false
    const idx = invCodes.value.findIndex((c) => c.id === res.code.id)
    if (idx >= 0) invCodes.value.splice(idx, 1, res.code)
  } catch {
    /* 拦截器已提示 */
  } finally {
    editLoading.value = false
  }
}

// --- 作废（可逆：只是把 is_active 翻成 false，行不删） ---
async function handleRevoke(rows: InvitationCodeRow[]) {
  if (!rows.length) return
  try {
    await ElMessageBox.confirm(
      `将作废 ${rows.length} 个邀请码？它们不能再被使用，但已产生的邀请关系不受影响。`,
      '作废邀请码',
      { type: 'warning', confirmButtonText: '作废', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  const res = await revokeInvitationCodes(rows.map((r) => r.id))
  ElMessage.success(res.message)
  await loadCodes()
}

// ===== 推广奖励：发放记录台账（开关与数值统一在「系统设置 → 邀请返利」里改） =====
const promo = ref<PromotionResponse | null>(null)
const promoLoading = ref(false)

async function loadPromotion() {
  promoLoading.value = true
  try {
    promo.value = await fetchPromotion({ limit: 100 })
  } catch {
    /* 拦截器已提示 */
  } finally {
    promoLoading.value = false
  }
}

const promoColumns: DataColumn[] = [
  { key: 'inviter_username', label: '邀请人', width: 130, mobile: 'title' },
  { key: 'invitee_username', label: '被邀请人', width: 130 },
  { key: 'reward_type_label', label: '类型', width: 120 },
  { key: 'reward_value', label: '数值', width: 90 },
  { key: 'created_at', label: '时间' },
]

const TYPE_LABELS: Record<string, string> = {
  checkin: '签到', invite: '邀请奖励', invitee: '受邀奖励', rebate: '充值返利',
  exchange: '兑换码', recharge: '充值', admin_grant: '管理发放', admin_deduct: '管理扣除',
}

/** 邀请码状态 → 徽章配色 */
function codeBadge(state: string): string {
  if (state === 'active') return 'au-badge-green'
  if (state === 'revoked') return 'au-badge-muted'
  if (state === 'expired') return 'au-badge-rose'
  return 'badge-warn'
}

function reloadAll() {
  load()
  loadCodes()
  loadPromotion()
  loadAuditSettings()
}

onMounted(reloadAll)
</script>

<template>
  <div class="admin-page invite-page">
    <PageHeader
      eyebrow="运营中心"
      title="邀请与积分"
      description="邀请码的生成与台账、积分流水都在这；返利规则等参数统一在「系统设置 → 邀请返利」里改。"
    >
      <template #actions>
        <el-button :icon="RefreshCw" :loading="loading" @click="reloadAll">刷新</el-button>
        <el-button :icon="Plus" @click="openAdjust">调整积分</el-button>
      </template>
    </PageHeader>

    <section class="stat-row" aria-label="邀请与积分概况">
      <StatTile label="全站积分存量" :value="stats?.total_points?.toLocaleString() ?? '—'" :icon="Coins" tone="accent" />
      <StatTile label="累计邀请关系" :value="stats?.invitations ?? '—'" :icon="UserPlus" />
      <StatTile label="本页邀请记录" :value="inviteTotal" :icon="Gift" hint="最新 100 条" />
      <StatTile label="本页返利合计" :value="rebateTotal" :icon="HandCoins" hint="当前积分流水页内" />
    </section>

    <!-- 页内锚点导航：5 个区块太长，点一下直达 -->
    <nav class="invite-anchors" aria-label="本页导航">
      <a href="#invite-codes">邀请码</a>
      <a href="#invite-records">邀请记录</a>
      <a href="#invite-logs">积分流水</a>
      <a href="#invite-audit">流水审计</a>
      <a href="#invite-promo">推广奖励</a>
    </nav>

    <!-- 邀请码管理（v2.44.0）：筛选在左、批量生成在右 -->
    <SectionCard
      id="invite-codes"
      title="邀请码"
      :icon="QrCode"
      :meta="`共 ${invSummary.total ?? 0} 张 · 可用 ${invSummary.active ?? 0} · 已用 ${invSummary.uses ?? 0} 次`"
      description="管理员维度发的渠道码，用户自己那张码（个人中心 → 邀请）不受影响。次数 0 = 不限，无有效期 = 永不过期，白名单留空 = 谁都能用。"
      flush
    >
      <div class="view-toolbar">
        <div class="view-toolbar__filters">
          <el-input
            v-model="invKeyword"
            class="f-search"
            placeholder="搜邀请码"
            clearable
            @change="onCodesFilter"
          >
            <template #prefix><Search :size="14" /></template>
          </el-input>
          <el-select v-model="invState" class="f-select" placeholder="全部状态" clearable @change="onCodesFilter">
            <el-option v-for="st in invStates" :key="st.value" :label="st.label" :value="st.value" />
          </el-select>
        </div>
        <div class="view-toolbar__actions">
          <el-button type="primary" :icon="Plus" @click="openGen">批量生成</el-button>
        </div>
      </div>

      <EmptyState v-if="codesError && !invCodes.length" :icon="AlertTriangle" title="邀请码加载失败" compact>
        <template #actions><el-button size="small" :loading="invLoading" @click="loadCodes">重试</el-button></template>
      </EmptyState>
      <DataTable
        v-else
        class="flush-table"
        :rows="invCodes"
        :columns="invColumns"
        :loading="invLoading"
        empty="还没有邀请码"
        empty-description="点「批量生成」给渠道或内测发一批。"
        :has-active-filters="!!(invKeyword || invState)"
        selectable
        v-model:selectedKeys="selectedKeys"
        :is-row-selectable="(row: InvitationCodeRow) => row.is_active"
        @clear-filters="clearCodesFilters"
      >
        <template #bulk-bar>
          <el-button type="danger" plain size="small" :loading="bulkRevokeBusy" @click="bulkRevokeCodes">批量作废</el-button>
        </template>
        <template #cell-code="{ row }">
          <span class="inv-code mono">{{ row.code }}</span>
        </template>
        <template #cell-owner_username="{ row }">
          <span class="user-name">{{ row.owner_username }}</span>
        </template>
        <template #cell-use_count="{ row }"><span class="num">{{ usesText(row) }}</span></template>
        <template #cell-reward_points="{ row }">
          <span v-if="!row.reward_points" class="faint">跟随全局</span>
          <span v-else class="amt-in">+{{ row.reward_points }}</span>
        </template>
        <template #cell-state_label="{ row }">
          <span class="au-badge" :class="codeBadge(row.state)">{{ row.state_label }}</span>
        </template>
        <template #cell-expires_at="{ row }">
          <span v-if="!row.expires_at" class="faint">永久</span>
          <span v-else class="num">{{ fmtTime(row.expires_at) }}</span>
        </template>
        <template #cell-whitelist="{ row }">
          <span v-if="!row.whitelist.length" class="faint">不限</span>
          <span v-else>{{ row.whitelist.join('、') }}</span>
        </template>
        <template #cell-actions="{ row }">
          <div class="row-actions">
            <el-button size="small" @click="openEdit(row)">编辑</el-button>
            <el-button v-if="row.is_active" size="small" type="danger" plain @click="handleRevoke([row])">作废</el-button>
          </div>
        </template>
      </DataTable>
    </SectionCard>

    <div class="ledger-grid">
      <!-- 邀请记录 -->
      <SectionCard id="invite-records" title="邀请记录" :icon="UserPlus" :meta="`最新 ${invitations.length} 条`" flush>
        <EmptyState v-if="invError && !invitations.length" :icon="AlertTriangle" title="邀请记录加载失败" compact>
          <template #actions><el-button size="small" :loading="loading" @click="loadOverview">重试</el-button></template>
        </EmptyState>
        <DataTable v-else class="flush-table" :rows="invitations" :columns="inviteColumns" :loading="loading" empty="暂无邀请记录">
          <template #cell-inviter="{ row }">
            <span class="user-name">{{ row.inviter }}</span>
          </template>
          <template #cell-invitee="{ row }">{{ row.invitee }}</template>
          <template #cell-reward_points="{ row }">
            <span class="amt-in">+{{ row.reward_points }}</span>
          </template>
          <template #cell-created_at="{ row }"><span class="num">{{ fmtTime(row.created_at) }}</span></template>
          <template #empty>
            <EmptyState compact :icon="UserPlus" title="暂无邀请记录" description="用户通过邀请码注册后会记在这里。" />
          </template>
        </DataTable>
      </SectionCard>

      <!-- 积分流水：类型筛选放在卡片标题行右侧，分页在底栏 -->
      <SectionCard id="invite-logs" title="积分流水" :icon="History" :meta="`共 ${logTotal} 条`" flush>
        <template #actions>
          <el-select
            v-model="logTypeFilter"
            class="f-select"
            placeholder="全部类型"
            clearable
            size="small"
            @change="logPage = 1; loadLogs()"
          >
            <el-option v-for="(label, key) in TYPE_LABELS" :key="key" :label="label" :value="key" />
          </el-select>
        </template>

        <EmptyState v-if="logsError && !logs.length" :icon="AlertTriangle" title="积分流水加载失败" compact>
          <template #actions><el-button size="small" :loading="logsLoading" @click="loadLogs">重试</el-button></template>
        </EmptyState>
        <DataTable v-else class="flush-table" :rows="logs" :columns="logColumns" :loading="logsLoading" empty="暂无积分流水"
          empty-description="邀请返利、签到、消费等积分变动都会记在这里。"
          :pagination="{ total: logTotal, page: logPage, pageSize: 30 }"
          @page-change="(p: number) => { logPage = p; loadLogs() }">
          <template #cell-username="{ row }">
            <span class="user-name">{{ row.username }}</span>
          </template>
          <template #cell-amount="{ row }">
            <span :class="row.amount > 0 ? 'amt-in' : 'amt-out'">
              {{ row.amount > 0 ? '+' : '' }}{{ row.amount }}
            </span>
          </template>
          <template #cell-type="{ row }">{{ TYPE_LABELS[row.type] || row.type }}</template>
          <template #cell-description="{ row }">
            <span v-if="!row.description" class="faint">—</span>
            <span v-else>{{ row.description }}</span>
          </template>
          <template #cell-balance_after="{ row }"><span class="num">{{ row.balance_after }}</span></template>
          <template #cell-audited="{ row }">
            <span v-if="row.audited" class="au-badge au-badge-green" title="该笔流水已写入 hash 链">已审计</span>
            <span v-else class="au-badge au-badge-muted" title="审计开启前的历史流水，无 hash">历史</span>
          </template>
          <template #cell-created_at="{ row }"><span class="num">{{ fmtTime(row.created_at) }}</span></template>
          <template #empty>
            <EmptyState compact :icon="History" :title="logTypeFilter ? '这个类型下还没有流水' : '暂无积分流水'" />
          </template>
        </DataTable>
      </SectionCard>
    </div>

    <!-- C3 流水审计：hash 链防篡改 -->
    <SectionCard
      id="invite-audit"
      title="流水审计"
      :icon="ShieldCheck"
      description="每笔积分流水写入时链接上一条的 hash，形成防篡改链。关闭后新流水不再写 hash（历史记录不受影响）；核验会逐条重算，发现篡改或断链即报警。"
    >
      <div class="audit-row">
        <el-switch
          :model-value="auditEnabled === 'true'"
          :loading="auditSaving"
          active-text="开启"
          inactive-text="关闭"
          @update:model-value="(v: string | number | boolean) => toggleAudit(v === true)"
        />
        <span class="faint">流水防篡改开关（points_audit_enabled）</span>
      </div>
      <div class="audit-verify">
        <el-select
          v-model="verifyUserId"
          class="f-select"
          placeholder="输入用户名搜索要核验的用户"
          filterable
          remote
          :remote-method="searchUsers"
          :loading="userSearching"
          clearable
        >
          <el-option v-for="u in userOptions" :key="u.id" :label="u.username" :value="u.id" />
        </el-select>
        <el-button type="primary" :icon="ShieldCheck" :loading="verifyLoading" @click="handleVerify">
          核验链条
        </el-button>
      </div>
      <div v-if="verifyResult" class="audit-result">
        <span v-if="verifyResult.ok" class="au-badge au-badge-green">链条完整</span>
        <span v-else class="au-badge au-badge-rose">发现异常</span>
        <span class="audit-meta">
          {{ verifyResult.username }}：共 {{ verifyResult.total }} 条 ·
          已校验 {{ verifyResult.verified }} 条 ·
          历史（无 hash）{{ verifyResult.legacy_skipped }} 条
        </span>
        <span v-if="!verifyResult.ok" class="audit-broken">
          断裂于记录 #{{ verifyResult.broken_at }}：{{ verifyResult.broken_reason }}
        </span>
      </div>
    </SectionCard>

    <!-- 推广奖励发放记录：台账。开关与数值统一在「系统设置 → 邀请返利」里改 -->
    <SectionCard
      id="invite-promo"
      :icon="Megaphone"
      description="邀请成功后在双向积分之外「另发」一笔，类型与数值全走配置。默认关闭——不手动打开就不会发；开关与数值统一在「系统设置 → 邀请返利」里改。"
      flush
    >
      <template #title>
        推广奖励
      </template>

      <EmptyState
        v-if="!promo && !promoLoading"
        :icon="AlertTriangle"
        title="推广奖励配置加载失败"
        compact
      >
        <template #actions><el-button size="small" @click="loadPromotion">重试</el-button></template>
      </EmptyState>

      <template v-else>
        <DataTable
          class="flush-table"
          :rows="promo?.rewards || []"
          :columns="promoColumns"
          :loading="promoLoading"
          empty="还没有推广奖励记录"
          empty-description="有人通过你的邀请链接下单后，奖励记录会出现在这里。"
        >
          <template #cell-inviter_username="{ row }">
            <span class="user-name">{{ row.inviter_username || '—' }}</span>
          </template>
          <template #cell-invitee_username="{ row }">{{ row.invitee_username }}</template>
          <template #cell-reward_type_label="{ row }">
            <span class="au-badge au-badge-info">{{ row.reward_type_label }}</span>
          </template>
          <template #cell-reward_value="{ row }">
            <span class="amt-in">+{{ row.reward_value }}</span>
          </template>
          <template #cell-created_at="{ row }"><span class="num">{{ fmtTime(row.created_at) }}</span></template>
          <template #empty>
            <EmptyState
              compact
              :icon="Megaphone"
              title="还没有推广奖励记录"
              description="开关默认关闭，打开后邀请成功才会记在这里。"
            />
          </template>
        </DataTable>
      </template>
    </SectionCard>

    <!-- 调整积分对话框 -->
    <el-dialog v-model="adjustVisible" title="手动调整用户积分" width="min(440px, 92vw)">
      <el-form ref="adjustFormRef" :model="adjustForm" :rules="adjustRules" label-position="top">
        <el-form-item label="用户" prop="user_id">
          <el-select
            v-model="adjustForm.user_id"
            filterable
            remote
            reserve-keyword
            :remote-method="searchUsers"
            :loading="userSearching"
            placeholder="搜索用户名"
            style="width: 100%"
          >
            <el-option
              v-for="u in userOptions"
              :key="u.id"
              :label="u.username"
              :value="u.id"
            />
          </el-select>
          <div class="form-hint">支持按用户名模糊搜索，无需再手填数据库 ID</div>
        </el-form-item>
        <el-form-item label="调整数量" prop="amount">
          <el-input-number v-model="adjustForm.amount" :step="10" style="width: 100%" />
          <div class="form-hint">正数发放 / 负数扣除（记账留审计）</div>
        </el-form-item>
        <el-form-item label="原因" prop="reason">
          <el-input v-model="adjustForm.reason" maxlength="100" placeholder="活动补偿等（选填）" />
        </el-form-item>
        <div v-if="selectedUser" class="adjust-preview">
          将对「{{ selectedUser }}」发放 / 扣减 {{ adjustForm.amount }} 积分
        </div>
      </el-form>
      <template #footer>
        <el-button @click="adjustVisible = false">取消</el-button>
        <el-button type="primary" :loading="adjustLoading" @click="handleAdjust">确认调整</el-button>
      </template>
    </el-dialog>

    <!-- 批量生成邀请码 -->
    <el-dialog v-model="genVisible" title="批量生成邀请码" width="min(460px, 92vw)">
      <el-form ref="genFormRef" :model="genForm" :rules="genRules" label-position="top">
        <el-form-item label="归属用户" prop="owner_user_id">
          <el-select
            v-model="genForm.owner_user_id"
            filterable
            remote
            reserve-keyword
            :remote-method="searchUsers"
            :loading="userSearching"
            placeholder="搜索用户名（这些码都算他的邀请）"
            style="width: 100%"
          >
            <el-option v-for="u in userOptions" :key="u.id" :label="u.username" :value="u.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="生成数量" prop="count">
          <el-input-number v-model="genForm.count" :min="1" :max="200" style="width: 100%" />
          <div class="form-hint">1–200 张，同一规格，每张码不同</div>
        </el-form-item>
        <el-form-item label="每码可用次数" prop="max_uses">
          <el-input-number v-model="genForm.max_uses" :min="0" :max="100000" style="width: 100%" />
          <div class="form-hint">0 = 不限次数</div>
        </el-form-item>
        <el-form-item label="每码奖励（积分）" prop="reward_points">
          <el-input-number v-model="genForm.reward_points" :min="0" :max="1000000" style="width: 100%" />
          <div class="form-hint">0 = 跟随全局「邀请人奖励」配置；填了就是这批码的专属奖励</div>
        </el-form-item>
        <el-form-item label="有效天数" prop="expires_days">
          <el-input-number v-model="genForm.expires_days" :min="0" :max="3650" style="width: 100%" />
          <div class="form-hint">0 = 永不过期；从生成那一刻开始算</div>
        </el-form-item>
        <el-form-item label="白名单（可选）">
          <el-input v-model="genForm.whitelist" placeholder="留空 = 谁都能用；多个用户名用逗号分隔"
                    type="textarea" :rows="2" />
          <div class="form-hint">填了就只有这些人能拿这个码注册（内测码 / 渠道码）</div>
        </el-form-item>
        <div v-if="genOwner" class="adjust-preview">将为「{{ genOwner }}」生成 {{ genForm.count }} 个邀请码</div>
      </el-form>
      <template #footer>
        <el-button @click="genVisible = false">取消</el-button>
        <el-button type="primary" :loading="genLoading" @click="handleGen">生成</el-button>
      </template>
    </el-dialog>

    <!-- 改单张邀请码 -->
    <el-dialog v-model="editVisible" :title="`编辑邀请码 ${editForm.code}`" width="min(460px, 92vw)">
      <el-form ref="editFormRef" :model="editForm" :rules="editRules" label-position="top">
        <el-form-item label="每码可用次数" prop="max_uses">
          <el-input-number v-model="editForm.max_uses" :min="0" :max="100000" style="width: 100%" />
          <div class="form-hint">0 = 不限次数</div>
        </el-form-item>
        <el-form-item label="奖励（积分）" prop="reward_points">
          <el-input-number v-model="editForm.reward_points" :min="0" :max="1000000" style="width: 100%" />
          <div class="form-hint">0 = 跟随全局「邀请人奖励」配置；填了就是这张码的专属奖励</div>
        </el-form-item>
        <el-form-item label="有效天数（从现在起）" prop="expires_days">
          <el-input-number v-model="editForm.expires_days" :min="0" :max="3650" style="width: 100%" />
          <div class="form-hint">0 = 永不过期</div>
        </el-form-item>
        <el-form-item label="白名单">
          <el-input v-model="editForm.whitelist" placeholder="留空 = 不限；多个用户名用逗号分隔"
                    type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="状态">
          <el-switch v-model="editForm.is_active" active-text="可用" inactive-text="已作废" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="editLoading" @click="handleEdit">保存</el-button>
      </template>
    </el-dialog>

  </div>
</template>

<style scoped>
.stat-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 12px;
}

/* ---------- 页内锚点导航 ---------- */
.invite-page {
  scroll-behavior: smooth;
}

.invite-anchors {
  display: flex;
  flex-direction: row;
  flex-wrap: wrap;
  gap: 8px;
  margin: 4px 0 16px;
}

.invite-anchors a {
  font-size: 12px;
  padding: 6px 12px;
  border-radius: 999px;
  border: 1px solid var(--au-border);
  color: var(--au-text-3);
  background: var(--au-surface);
  text-decoration: none;
  transition: border-color 0.2s ease, color 0.2s ease;
}

.invite-anchors a:hover {
  border-color: var(--au-primary);
  color: var(--au-text);
}

.invite-page .au-section[id] {
  scroll-margin-top: 76px;
}

/* ---------- 工具条：筛选在左、动作在右 ---------- */
.view-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px 12px;
  flex-wrap: wrap;
  padding: 4px 20px 14px;
  border-bottom: 1px solid var(--au-border);
}

.view-toolbar__filters { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; flex: 1 1 auto; min-width: 0; }
.view-toolbar__actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.f-search { width: 200px; }
.f-select { width: 140px; }

.flush-table :deep(.dt-cards) { padding: 12px 12px 8px; }
.card-pager { display: flex; justify-content: flex-end; flex-wrap: wrap; row-gap: 8px; }

/* 邀请记录 + 积分流水并排；窄屏叠起来 */
.ledger-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1.35fr);
  gap: 16px;
  align-items: start;
}

/* ---------- 单元格 ---------- */
.user-name { font-weight: 600; color: var(--au-text); }
.num { font-variant-numeric: tabular-nums; }
.faint { color: var(--au-text-4); }
.amt-in { color: var(--au-success); font-weight: 600; font-variant-numeric: tabular-nums; }
.amt-out { color: var(--au-danger); font-weight: 600; font-variant-numeric: tabular-nums; }
.inv-code { font-size: 13px; color: var(--au-text); letter-spacing: 0.04em; }
.row-actions { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.badge-warn { background: var(--au-warning-soft); color: var(--au-warning); border-color: var(--au-warning-border); }

.field-suffix { font-size: 12px; color: var(--au-text-3); }

/* ---------- 对话框 ---------- */
.adjust-preview {
  padding: 10px 12px;
  border-radius: var(--au-r-md);
  background: var(--au-primary-soft);
  border: 1px solid var(--au-primary-border);
  color: var(--au-primary);
  font-size: 12px;
}

/* ---------- C3 流水审计 ---------- */
.audit-row { display: flex; align-items: center; gap: 12px; padding: 4px 0 12px; }
.audit-verify { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; padding-bottom: 4px; }
.audit-verify .f-select { width: 260px; }
.audit-result { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; padding-top: 10px; font-size: 13px; }
.audit-meta { color: var(--au-text-2); }
.audit-broken { color: var(--au-danger); }

@media (max-width: 1000px) {
  .ledger-grid { grid-template-columns: minmax(0, 1fr); }
}

@media (max-width: 768px) {
  .view-toolbar { padding: 2px 16px 12px; }
  .view-toolbar__filters > .f-search,
  .view-toolbar__filters > .f-select { flex: 1 1 140px; width: auto; }
  .view-toolbar__actions { width: 100%; }
  .view-toolbar__actions > .el-button { flex: 1; }
  .card-pager { justify-content: center; }
}
</style>
