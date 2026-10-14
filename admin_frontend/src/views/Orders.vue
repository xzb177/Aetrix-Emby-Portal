<script setup lang="ts">
/**
 * 运营管理：经济总览 + 订单管理 + 人工补单
 *
 * v2.54（暗房影院）：PageHeader + StatTile 总览（待支付 > 0 才染警示、可点进带筛选的列表）+
 * flush SectionCard 订单表（筛选在左、查询在右，分页收进卡片底栏）；加载失败给可重试的错误态。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { toAdminError } from '@/utils/errorMessage'
import {
  RefreshCw, Search, CircleCheck, Wallet, Ban, Undo2, ReceiptText, Coins, CalendarCheck, Clock3,
  Gift, UserPlus, AlertTriangle,
} from 'lucide-vue-next'
import { EmptyState, PageHeader, SectionCard, StatTile } from '@/components/ui'
import DataTable from '@/components/DataTable.vue'
import type { DataColumn } from '@/components/DataTable.vue'
import { useQueryFilter } from '@/composables/useQueryFilter'
import { useTableSelection } from '@/composables/useTableSelection'
import { runBulkOperation, bulkSummaryText, BULK_LIMIT } from '@/utils/runBulkOperation'
import type { RowKey } from '@/composables/useTableSelection'

/** 手机卡片：商品名为标题，金额/状态/用户/时间做键值行，订单号收进详情（太长会撑满一行） */
const columns: DataColumn[] = [
  { key: 'item_name', label: '商品', minWidth: 170, mobile: 'title' },
  { key: 'order_id', label: '订单号', width: 220, mobile: 'hide' },
  { key: 'kind', label: '类型', width: 90 },
  { key: 'amount', label: '金额', width: 110 },
  { key: 'username', label: '用户', width: 120 },
  { key: 'status', label: '状态', width: 96 },
  { key: 'payment_method', label: '支付方式', width: 110, mobile: 'hide' },
  { key: 'created_at', label: '创建时间', width: 160 },
  // 补单 / 关单 / 退款收进订单详情弹窗：行里只留一个入口（状态不同则可用动作不同）
  { key: 'actions', label: '操作', width: 100, fixed: 'right', align: 'right' },
]
import {
  fetchEconomyStats,
  fetchEconomyOrders,
  markOrderPaid,
  closeOrder,
  refundOrder,
  fetchRefundRecords,
  type EconomyStats,
  type OrderRow,
  type RefundRecord,
} from '@/api/economy'

const loading = ref(false)
const stats = ref<EconomyStats | null>(null)
const orders = ref<OrderRow[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = 20
const statusFilter = ref('')
const kindFilter = ref('')
const search = ref('')
/** 订单主表加载失败（不是「没有订单」）：表格位置换成可重试的错误态 */
const loadError = ref('')
// 深链：仪表盘 KPI / 命令面板带筛选过来（Phase 5，如 /orders?status=pending）
// 回调里回到第一页：带着筛选翻到第 5 页是空页，深链不该把人送到空页上
useQueryFilter(statusFilter, 'status', () => { page.value = 1; load() })
useQueryFilter(kindFilter, 'kind', () => { page.value = 1; load() })
useQueryFilter(search, 'search', () => { page.value = 1; load() })

/**
 * 经济总览与筛选 / 翻页无关：单独拉，翻页、改筛选时不再重复请求统计接口。
 * 失败只隐藏总览，不影响订单主表。
 */
async function loadStats() {
  try {
    stats.value = await fetchEconomyStats()
  } catch {
    // 保留上一次的总览
  }
}

/** 请求序号：连续改筛选 / 翻页时只采纳最后一次请求，避免旧响应覆盖新结果 */
let loadSeq = 0
/** 最近一次实际查询用的关键字：防抖回调据此跳过重复查询（深链改 search 时 load 已经发过） */
let lastSearch = ''

async function load() {
  const seq = ++loadSeq
  loading.value = true
  loadError.value = ''
  lastSearch = search.value
  try {
    const o = await fetchEconomyOrders({
      status_filter: statusFilter.value || undefined,
      kind: kindFilter.value || undefined,
      search: search.value || undefined,
      limit: pageSize,
      offset: (page.value - 1) * pageSize,
    })
    if (seq !== loadSeq) return
    // 关单 / 退款后当前页可能被筛空：回到最后一页，而不是停在空页上显示「没有订单」
    if (!o.orders.length && o.total > 0 && page.value > 1) {
      page.value = Math.max(1, Math.ceil(o.total / pageSize))
      void load()
      return
    }
    orders.value = o.orders
    total.value = o.total
  } catch (e: unknown) {
    if (seq !== loadSeq) return
    loadError.value = toAdminError(e, '加载失败')
    ElMessage.error(loadError.value)
  } finally {
    if (seq === loadSeq) loading.value = false
  }
}

/** 「刷新」：总览与订单一起重拉 */
function refreshAll() {
  loadStats()
  load()
}

/**
 * 行选择 + 批量标记已支付 / 批量退款（v2.58）
 * - 选择是纯前端状态，筛选提交时清空；翻页保留
 * - rowKey 是 order_id（string）
 */
const selection = useTableSelection()
/** 模板 v-model 用的可写代理：嵌套 ref 在模板里不会自动解包，直接绑 selection.selectedKeys 会传 Ref 对象 */
const selectedKeys = computed<RowKey[]>({
  get: () => selection.selectedKeys.value,
  set: (v) => { selection.selectedKeys.value = v },
})
const bulkBusy = ref(false)

function selectedOrders(): OrderRow[] {
  const ids = new Set(selection.selectedKeys.value as string[])
  return orders.value.filter((o) => ids.has(o.order_id))
}

/** 批量标记已支付：只对选中里的待支付订单执行 */
async function bulkMarkPaid() {
  if (bulkBusy.value) return
  const pendings = selectedOrders().filter((o) => o.status === 'pending')
  if (!pendings.length) {
    ElMessage.warning('所选项中没有待支付订单')
    return
  }
  if (pendings.length > BULK_LIMIT) {
    ElMessage.warning(`一次最多操作 ${BULK_LIMIT} 项`)
    return
  }
  try {
    await ElMessageBox.confirm(
      `确认将 ${pendings.length} 个待支付订单标记为已支付并履约？用于线下收款或回调丢失的补单。`,
      '批量人工补单',
      { type: 'warning', confirmButtonText: '确认补单', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  if (bulkBusy.value) return
  bulkBusy.value = true
  try {
    const summary = await runBulkOperation(pendings.map((o) => o.order_id), (id) => markOrderPaid(id))
    // 失败保留选中态可重试
    selection.selectedKeys.value = summary.failedKeys as RowKey[]
    ElMessage[summary.failed ? 'warning' : 'success'](bulkSummaryText(summary, '补单'))
    // 不调 afterAction：它会关掉订单详情弹窗；这里直接刷新三块数据
    load()
    loadStats()
    loadRecords()
  } finally {
    bulkBusy.value = false
  }
}

/** 批量退款对话框：独立于单条退款对话框（refundDialog），字段结构一致，现有单条逻辑不动 */
const batchRefundDialog = ref({
  visible: false,
  ids: [] as string[],
  reason: '',
  revoke: true,
  allowNegative: false,
  reverseRebate: true,
})

function openBatchRefund() {
  if (bulkBusy.value) return
  const ids = selectedOrders().filter((o) => o.status === 'paid').map((o) => o.order_id)
  if (!ids.length) {
    ElMessage.warning('所选项中没有已支付订单')
    return
  }
  if (ids.length > BULK_LIMIT) {
    ElMessage.warning(`一次最多操作 ${BULK_LIMIT} 项`)
    return
  }
  batchRefundDialog.value = {
    visible: true, ids, reason: '', revoke: true, allowNegative: false, reverseRebate: true,
  }
}

async function submitBatchRefund() {
  if (bulkBusy.value) return
  const d = batchRefundDialog.value
  if (!d.reason.trim()) {
    ElMessage.warning('请填写退款原因（对账与客服复盘都要用）')
    return
  }
  bulkBusy.value = true
  try {
    // 逐条执行退款，失败的不自动重试
    const summary = await runBulkOperation(
      d.ids,
      (id) => refundOrder(id, {
        reason: d.reason.trim(),
        revoke_entitlement: d.revoke,
        allow_negative: d.allowNegative,
        reverse_rebate: d.reverseRebate,
      }),
      { concurrency: 2 },
    )
    selection.selectedKeys.value = summary.failedKeys as RowKey[]
    ElMessage[summary.failed ? 'warning' : 'success'](bulkSummaryText(summary, '退款'))
    batchRefundDialog.value.visible = false
    load()
    loadStats()
    loadRecords()
  } finally {
    bulkBusy.value = false
  }
}

/** 筛选变化 / 点「查询」：回到第一页再拉 */
function query() {
  selection.clear()
  page.value = 1
  load()
}

/** 搜索框输入防抖：停手 350ms 自动查询（回车 / 清空仍立即查询） */
let searchTimer: ReturnType<typeof setTimeout> | undefined
watch(search, (value) => {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    if (value !== lastSearch) query()
  }, 350)
})
onBeforeUnmount(() => clearTimeout(searchTimer))

const hasFilter = computed(() => !!(statusFilter.value || kindFilter.value || search.value))

function resetFilters() {
  selection.clear()
  statusFilter.value = ''
  kindFilter.value = ''
  search.value = ''
  query()
}

// ==================== 关单 / 退款（v2.9.0） ====================
// 以前订单只能「标记已支付」：线下多转一笔、点错套餐都没法处理，只能去改数据库。
// 现在：待支付 → 关闭（作废）；已支付 → 退款（默认按账本回滚积分/会员天数）。

/** 状态标签：四种状态的颜色与文案集中一处，表格与筛选共用 */
const STATUS_META: Record<string, { text: string; type: 'success' | 'warning' | 'info' | 'danger' }> = {
  paid: { text: '已支付', type: 'success' },
  pending: { text: '待支付', type: 'warning' },
  refunded: { text: '已退款', type: 'danger' },
  closed: { text: '已关闭', type: 'info' },
}

function statusText(status: string): string {
  return STATUS_META[status]?.text || status
}

function statusType(status: string) {
  return STATUS_META[status]?.type || 'info'
}

const records = ref<RefundRecord[]>([])

async function loadRecords() {
  try {
    records.value = (await fetchRefundRecords(8)).records
  } catch {
    // 记录区加载失败不该影响订单主表
  }
}

// ==================== 订单详情弹窗（v2.29.0） ====================
// 以前行尾直接摆着「补单 / 关闭」两个按钮，点下去只有一句确认框；现在一个「详情」入口，
// 弹窗里先看全订单（订单号 / 用户 / 金额 / 支付方式 / 创建与支付时间），再决定怎么处理。
const detail = ref({ visible: false, row: null as OrderRow | null })

function openDetail(row: OrderRow) {
  detail.value = { visible: true, row }
}

/** 动作后刷新订单与退款记录，并把弹窗里的订单换成最新快照 */
async function afterAction(orderId: string) {
  await Promise.all([load(), loadRecords(), loadStats()])
  const fresh = orders.value.find((o) => o.order_id === orderId)
  if (fresh) {
    detail.value.row = fresh
  } else {
    // 当前筛选下已不包含它（例如只看待支付时补了单）：关掉并说明
    detail.value.visible = false
    ElMessage.info('已完成，该订单不再符合当前筛选条件')
  }
}

async function handleClose(row: OrderRow) {
  try {
    await ElMessageBox.confirm(
      `订单 ${row.order_id} 将被作废；它还没有支付，用户不会收到通知。`,
      '关闭订单',
      {
        confirmButtonText: '关闭这个订单',
        cancelButtonText: '取消',
        type: 'warning',
        confirmButtonClass: 'el-button--danger',
      },
    )
  } catch {
    return
  }
  try {
    await closeOrder(row.order_id)
    ElMessage.success('订单已关闭')
    await afterAction(row.order_id)
  } catch (e: unknown) {
    ElMessage.error(toAdminError(e, '关单失败'))
  }
}

// 退款对话框：原因 + 两个开关。默认回滚权益（退款不退权益=白送），
// 余额不够时后端会拒绝并回报余额，管理员可以显式勾选「允许余额为负」再退。
const refundDialog = ref({
  visible: false,
  row: null as OrderRow | null,
  reason: '',
  revoke: true,
  allowNegative: false,
  reverseRebate: true,
})
const refundSaving = ref(false)

function openRefund(row: OrderRow) {
  refundDialog.value = {
    visible: true, row, reason: '', revoke: true, allowNegative: false, reverseRebate: true,
  }
}

/** 从详情弹窗进退款：先关详情，避免两个弹窗叠在一起 */
function refundFromDetail(row: OrderRow) {
  detail.value.visible = false
  openRefund(row)
}

async function submitRefund() {
  const row = refundDialog.value.row
  if (!row) return
  const d = refundDialog.value
  if (!d.reason.trim()) {
    ElMessage.warning('请填写退款原因（对账与客服复盘都要用）')
    return
  }
  refundSaving.value = true
  try {
    const res = await refundOrder(row.order_id, {
      reason: d.reason.trim(),
      revoke_entitlement: d.revoke,
      allow_negative: d.allowNegative,
      reverse_rebate: d.reverseRebate,
    })
    const bits: string[] = []
    if (res.revoked_points) bits.push(`扣回 ${res.revoked_points} 积分`)
    if (res.rebate_reversed) bits.push(`撤回返利 ${res.rebate_reversed} 积分`)
    if (res.revoked_days) bits.push(`回滚 ${res.revoked_days} 天`)
    if (res.cancelled) bits.push('该订阅已撤销')
    ElMessage.success(`退款完成${bits.length ? '：' + bits.join('、') : ''}`)
    refundDialog.value.visible = false
    await afterAction(row.order_id)
  } catch (e: unknown) {
    // 余额不足时后端给出可读原因，原样展示（引导勾选「允许余额为负」）
    ElMessage.error(toAdminError(e, '退款失败'))
  } finally {
    refundSaving.value = false
  }
}

async function handleMarkPaid(row: OrderRow) {
  try {
    await ElMessageBox.confirm(
      `确认将订单 ${row.order_id} 标记为已支付并履约？用于线下收款或回调丢失的补单。`,
      '人工补单',
      { confirmButtonText: '确认补单', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await markOrderPaid(row.order_id)
    ElMessage.success('补单成功，已发货')
    await afterAction(row.order_id)
  } catch (e: unknown) {
    ElMessage.error(toAdminError(e, '补单失败'))
  }
}

function fmtTime(iso?: string | null) {
  return iso ? iso.slice(0, 19).replace('T', ' ') : '—'
}

onMounted(() => {
  load()
  loadStats()
  loadRecords()
})
</script>

<template>
  <div class="admin-page orders-page">
    <PageHeader
      eyebrow="运营中心"
      title="订单"
      description="充值 / 订阅订单、营收统计与人工补单。点「处理 / 详情」补单、关单或退款。"
    >
      <template #actions>
        <el-button :icon="RefreshCw" :loading="loading" @click="refreshAll">刷新</el-button>
      </template>
    </PageHeader>

    <!-- 经济总览：数字走正文色，只有「待支付 > 0」染警示；累计营收是全页唯一的琥珀数字 -->
    <section v-if="stats" class="stat-row" aria-label="经济总览">
      <StatTile label="累计营收" :value="stats.orders.revenue.toFixed(2)" suffix="¥" :icon="Wallet" tone="accent" />
      <StatTile
        label="待支付订单"
        :value="stats.orders.pending"
        :icon="Clock3"
        :tone="stats.orders.pending > 0 ? 'warn' : 'plain'"
        :to="stats.orders.pending > 0 ? '/orders?status=pending' : undefined"
        :hint="stats.orders.pending > 0 ? '点开只看待支付' : '没有待处理的'"
      />
      <StatTile label="累计充值积分" :value="stats.total_points.toLocaleString()" :icon="Coins" />
      <StatTile label="今日签到人次" :value="stats.checkins_today" :icon="CalendarCheck" />
      <StatTile
        label="兑换码已用"
        :value="stats.exchange_codes.used"
        :suffix="`/ ${stats.exchange_codes.total}`"
        :icon="Gift"
        to="/exchange-codes"
      />
      <StatTile label="邀请总数" :value="stats.invitations" :icon="UserPlus" to="/invitations" />
    </section>

    <!-- 订单表：筛选在左、查询在右；桌面表格 / 手机卡片；分页在卡片底栏 -->
    <SectionCard title="全部订单" :icon="ReceiptText" :meta="total ? `共 ${total} 条` : ''" flush>
      <div class="view-toolbar">
        <div class="view-toolbar__filters">
          <el-input
            v-model="search"
            class="f-search"
            placeholder="搜索订单号 / 用户名"
            clearable
            @keyup.enter="query"
            @clear="query"
          >
            <template #prefix><Search :size="14" /></template>
          </el-input>
          <el-select v-model="kindFilter" class="f-select" placeholder="类型" clearable @change="query">
            <el-option label="充值" value="recharge" />
            <el-option label="订阅" value="subscription" />
          </el-select>
          <el-select v-model="statusFilter" class="f-select" placeholder="状态" clearable @change="query">
            <el-option label="待支付" value="pending" />
            <el-option label="已支付" value="paid" />
            <el-option label="已退款" value="refunded" />
            <el-option label="已关闭" value="closed" />
          </el-select>
        </div>
        <div class="view-toolbar__actions">
          <el-button v-if="hasFilter" text @click="resetFilters">清空筛选</el-button>
          <el-button type="primary" :icon="Search" @click="query">查询</el-button>
        </div>
      </div>

      <EmptyState
        v-if="loadError && !orders.length"
        :icon="AlertTriangle"
        title="订单加载失败"
        :description="loadError"
      >
        <template #actions><el-button :loading="loading" @click="load">重试</el-button></template>
      </EmptyState>

      <DataTable
        v-else
        class="flush-table"
        :rows="orders"
        :columns="columns"
        :loading="loading"
        row-key="order_id"
        selectable
        v-model:selectedKeys="selectedKeys"
        empty="暂无订单"
        empty-description="用户充值或订阅后，订单会出现在这里。"
        :has-active-filters="hasFilter"
        :pagination="{ total, page, pageSize }"
        @page-change="(p: number) => { page = p; load() }"
        @clear-filters="resetFilters"
      >
        <template #bulk-bar>
          <el-button type="success" plain size="small" :loading="bulkBusy" @click="bulkMarkPaid">批量标记已支付</el-button>
          <el-button type="danger" plain size="small" :loading="bulkBusy" @click="openBatchRefund">批量退款</el-button>
        </template>
        <template #cell-item_name="{ row }">{{ row.item_name }}</template>

        <template #cell-order_id="{ row }">
          <span class="mono">{{ row.order_id }}</span>
        </template>

        <template #cell-kind="{ row }">
          <el-tag :type="row.kind === 'recharge' ? 'success' : 'primary'" size="small">
            {{ row.kind === 'recharge' ? '充值' : '订阅' }}
          </el-tag>
        </template>

        <template #cell-amount="{ row }">
          <span class="amount">¥ {{ Number(row.amount).toFixed(2) }}</span>
        </template>

        <template #cell-username="{ row }">{{ row.username }}</template>

        <template #cell-status="{ row }">
          <el-tag :type="statusType(row.status)" size="small">{{ statusText(row.status) }}</el-tag>
        </template>

        <template #cell-payment_method="{ row }">{{ row.payment_method || '—' }}</template>

        <template #cell-created_at="{ row }">{{ fmtTime(row.created_at) }}</template>

        <template #cell-actions="{ row }">
          <!-- 一个入口：补单 / 关单 / 退款 都在详情弹窗里（原来这行是两个动作按钮） -->
          <el-button size="small" :type="row.status === 'pending' ? 'primary' : 'default'" plain @click="openDetail(row)">
            {{ row.status === 'pending' ? '处理' : '详情' }}
          </el-button>
        </template>

      </DataTable>
    </SectionCard>

    <!-- 退款 / 关单记录：订单表只有状态标签，说不清「为什么退的」 -->
    <SectionCard
      v-if="records.length"
      title="最近的退款 / 关单"
      :icon="Undo2"
      meta="原因会同时写进操作审计"
      flush
    >
      <ul class="records">
        <li v-for="r in records" :key="r.order_id + r.status" class="record">
          <span class="mono record-id">{{ r.order_id }}</span>
          <span class="au-badge" :class="r.status === 'refunded' ? 'au-badge-rose' : 'au-badge-muted'">
            {{ r.status === 'refunded' ? '已退款' : '已关闭' }}
          </span>
          <span class="record-user">{{ r.username }}</span>
          <span class="record-reason">{{ r.reason || '（未填原因）' }}</span>
          <span class="record-time">{{ fmtTime(r.at) }}</span>
        </li>
      </ul>
    </SectionCard>

    <!-- 退款：默认回滚权益，余额不够时可显式允许扣成负数 -->
    <el-dialog v-model="refundDialog.visible" title="订单退款" width="min(480px, 92vw)">
      <el-form label-position="top">
        <el-form-item label="订单">
          <span class="dialog-order">
            <span class="mono">{{ refundDialog.row?.order_id }}</span>
            <em class="muted">（{{ refundDialog.row?.item_name }} · ¥{{ Number(refundDialog.row?.amount ?? 0).toFixed(2) }}）</em>
          </span>
        </el-form-item>
        <el-form-item label="退款原因（必填，写入审计）">
          <el-input v-model="refundDialog.reason" placeholder="例如：线下重复付款 / 用户申请退款" />
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="refundDialog.revoke">
            回滚权益（充值扣回积分 / 订阅回滚天数）
          </el-checkbox>
          <p class="opt-hint">
            取消勾选则只记账、不动权益——用于客服补偿这类“钱退了但东西留着”的场景。
          </p>
        </el-form-item>
        <el-form-item v-if="refundDialog.revoke && refundDialog.row?.kind === 'recharge'">
          <el-checkbox v-model="refundDialog.reverseRebate">同时撤回邀请人返利</el-checkbox>
          <p class="opt-hint">
            返利是这笔订单产生的；只退买家不退返利，等于站点为一次退款付两遍钱。
          </p>
        </el-form-item>
        <el-form-item v-if="refundDialog.revoke && refundDialog.row?.kind === 'recharge'">
          <el-checkbox v-model="refundDialog.allowNegative">用户余额不足时允许扣成负数</el-checkbox>
          <p class="opt-hint">
            不勾选时余额不够会被拒绝（并回报当前余额），避免悄悄把账户扣成负数。
          </p>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="refundDialog.visible = false">取消</el-button>
        <el-button type="danger" :loading="refundSaving" @click="submitRefund">退这笔款</el-button>
      </template>
    </el-dialog>

    <!-- 批量退款：字段与单条退款对话框一致（原因必填 + 三个开关），单条逻辑不动 -->
    <el-dialog v-model="batchRefundDialog.visible" :title="`批量退款（${batchRefundDialog.ids.length} 个订单）`" width="min(480px, 92vw)">
      <p class="muted" style="margin-bottom: 12px">将逐条执行退款，失败的不自动重试。</p>
      <el-form label-position="top">
        <el-form-item label="退款原因（必填，写入审计）">
          <el-input v-model="batchRefundDialog.reason" placeholder="例如：线下重复付款 / 用户申请退款" />
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="batchRefundDialog.revoke">
            回滚权益（充值扣回积分 / 订阅回滚天数）
          </el-checkbox>
          <p class="opt-hint">
            取消勾选则只记账、不动权益——用于客服补偿这类"钱退了但东西留着"的场景。
          </p>
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="batchRefundDialog.reverseRebate">同时撤回邀请人返利</el-checkbox>
          <p class="opt-hint">
            返利是这笔订单产生的；只退买家不退返利，等于站点为一次退款付两遍钱。
          </p>
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="batchRefundDialog.allowNegative">用户余额不足时允许扣成负数</el-checkbox>
          <p class="opt-hint">
            不勾选时余额不够会被拒绝（并回报当前余额），避免悄悄把账户扣成负数。
          </p>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button :disabled="bulkBusy" @click="batchRefundDialog.visible = false">取消</el-button>
        <el-button type="danger" :loading="bulkBusy" @click="submitBatchRefund">退这批款</el-button>
      </template>
    </el-dialog>

    <!--
      订单详情（弹窗）：订单号 / 用户 / 金额 / 支付方式 / 创建与支付时间全在这里，
      可用的处理动作随状态变化（待支付：补单或关单；已支付：退款），退款仍走专门的退款弹窗
      （勾选项多，放在同一层会看不清）。
    -->
    <el-dialog v-model="detail.visible" title="订单详情" width="min(520px, 92vw)">
      <div v-if="detail.row" class="od-body">
        <div class="od-head">
          <el-tag :type="statusType(detail.row.status)" size="small">
            {{ statusText(detail.row.status) }}
          </el-tag>
          <span class="od-amount">¥ {{ Number(detail.row.amount).toFixed(2) }}</span>
        </div>

        <div class="kv-list">
          <div class="kv-row"><span class="kv-key">订单号</span><span class="kv-value mono">{{ detail.row.order_id }}</span></div>
          <div class="kv-row"><span class="kv-key">商品</span><span class="kv-value">{{ detail.row.item_name }}</span></div>
          <div class="kv-row"><span class="kv-key">类型</span>
            <span class="kv-value">{{ detail.row.kind === 'recharge' ? '充值' : '订阅' }}</span>
          </div>
          <div class="kv-row"><span class="kv-key">用户</span><span class="kv-value">{{ detail.row.username }}</span></div>
          <div class="kv-row"><span class="kv-key">支付方式</span>
            <span class="kv-value">{{ detail.row.payment_method || '—' }}</span>
          </div>
          <div class="kv-row"><span class="kv-key">创建时间</span><span class="kv-value">{{ fmtTime(detail.row.created_at) }}</span></div>
          <div class="kv-row"><span class="kv-key">支付时间</span>
            <span class="kv-value">{{ detail.row.paid_at ? fmtTime(detail.row.paid_at) : '未支付' }}</span>
          </div>
        </div>

        <p class="od-hint">
          <template v-if="detail.row.status === 'pending'">
            补单用于线下收款或支付回调丢失（会按订单内容直接履约）；关闭则作废订单，用户不会收到通知。
          </template>
          <template v-else-if="detail.row.status === 'paid'">
            退款会按账本回滚权益（可逐项勾选），原因会写进操作审计与下面的退款记录。
          </template>
          <template v-else>该订单已{{ statusText(detail.row.status) }}，无可执行的动作。</template>
        </p>
      </div>

      <template #footer>
        <el-button @click="detail.visible = false">关闭</el-button>
        <template v-if="detail.row?.status === 'pending'">
          <el-button @click="detail.row && handleClose(detail.row)">
            <Ban :size="13" style="margin-right: 4px" />关闭订单
          </el-button>
          <el-button type="success" plain @click="detail.row && handleMarkPaid(detail.row)">
            <CircleCheck :size="13" style="margin-right: 4px" />人工补单
          </el-button>
        </template>
        <el-button
          v-else-if="detail.row?.status === 'paid'"
          type="danger"
          plain
          @click="detail.row && refundFromDetail(detail.row)"
        >
          <Undo2 :size="13" style="margin-right: 4px" />退款
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
/* ---------- 总览瓦片 ---------- */
.stat-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 12px;
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

.view-toolbar__filters {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  flex: 1 1 auto;
  min-width: 0;
}

.view-toolbar__actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.f-search { width: 240px; }
.f-select { width: 130px; }

/* flush 卡片里的表格 / 手机卡片：表格贴边，手机卡片留出边距 */
.flush-table :deep(.dt-cards) { padding: 12px 12px 8px; }
.card-pager { display: flex; justify-content: flex-end; flex-wrap: wrap; row-gap: 8px; }

.amount { font-weight: 600; font-variant-numeric: tabular-nums; color: var(--au-text); }
.dialog-order { display: inline-flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.dialog-order em { font-style: normal; font-size: 12px; }
.opt-hint { margin: 2px 0 0; font-size: 12px; color: var(--au-text-3); line-height: 1.5; }

/* ---------- 订单详情弹窗：详情用全局 .kv-list，只补金额行与说明 ---------- */
.od-body { display: flex; flex-direction: column; gap: 12px; }
.od-body .kv-row .kv-value { text-align: left; }
.od-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.od-amount { font-size: 18px; font-weight: 700; font-variant-numeric: tabular-nums; color: var(--au-text); }
.od-hint {
  margin: 0;
  padding: 10px 12px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--au-text-3);
  background: var(--au-surface-2);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-md);
}

/* ---------- 退款 / 关单记录：贴边列表，发丝线分行 ---------- */
.records { list-style: none; margin: 0; padding: 0; }
.record {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding: 10px 20px;
  font-size: 12.5px;
  color: var(--au-text-2);
}
.record + .record { border-top: 1px solid var(--au-border); }
.record-id { color: var(--au-text); }
.record-user { color: var(--au-text-3); }
.record-reason { flex: 1 1 160px; min-width: 120px; }
.record-time { font-variant-numeric: tabular-nums; color: var(--au-text-4); }

@media (max-width: 768px) {
  .view-toolbar { padding: 2px 16px 12px; }
  .view-toolbar__filters > .f-search { flex: 1 1 100%; width: auto; }
  .view-toolbar__filters > .f-select { flex: 1 1 120px; width: auto; }
  .view-toolbar__actions { width: 100%; justify-content: flex-end; }
  .card-pager { justify-content: center; }
  .record { padding: 10px 16px; }
}
</style>
