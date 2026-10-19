<script setup lang="ts">
/**
 * 用户管理
 *
 * v2.4.0 重构：
 * - 新增「用户 360°」详情抽屉（资料 / 会员 / 积分 / 订单 / 邀请 / 签到 / 观看）
 * - 授予会员、调整积分、重置密码、发送消息改为正规对话框（原先靠输入序号）
 * - 行内操作收敛为「详情 + 更多」下拉，表格不再横向堆 6 个按钮
 *
 * v2.6.10：手机（≤640px）下表格列收窄并隐藏「注册时间」（详情抽屉里本来就有），
 * 否则 375px 宽的屏幕上列宽合计近 1000px，必须先横向拖很远才能看到操作列。
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  CalendarCheck, CircleCheck, CircleSlash, Coins, Crown, Download, Eye, EyeOff, Film, Gift,
  History, KeyRound, Mail, Megaphone, MonitorSmartphone, MoreHorizontal, PlayCircle, RefreshCw, Search,
  Server, ShieldCheck, Smartphone, UserX, Users as UsersIcon, Wallet, Waypoints,
} from 'lucide-vue-next'
import { EmptyState, PageHeader, SectionCard, StatTile } from '@/components/ui'
import { useBreakpoint } from '@/composables/useBreakpoint'
import {
  broadcastMessage, extendSubscription, fetchDevices, fetchPlans, fetchUserDetail, fetchUsers,
  fetchUserGrants, grantSubscription, removeDevice, resetUserPassword, sendUserMessage, setDeviceBlocked,
  updateUser, type PlanRow,
} from '@/api/admin'
import { adjustUserPoints } from '@/api/economy'
import { bulkExtendWelfare, grantWelfare, revokeWelfare } from '@/api/welfare'
import type { AdminUserRow, DeviceRow, UserDetail, UserGrantCard, UserGrants } from '@/types'
import { useAuthStore } from '@/stores/auth'
import DataTable from '@/components/DataTable.vue'
import type { DataColumn } from '@/components/DataTable.vue'
import { useTableSelection } from '@/composables/useTableSelection'
import { runBulkOperation, bulkSummaryText, BULK_LIMIT } from '@/utils/runBulkOperation'
import type { RowKey } from '@/composables/useTableSelection'

const auth = useAuthStore()
/** 手机上详情抽屉铺满整屏（620px 抽屉在 390px 屏幕上会被裁掉） */
const { isPhone } = useBreakpoint()
const drawerSize = computed(() => (isPhone.value ? '100%' : '620px'))

/** 用户列表：手机端用户名做标题，注册时间隐藏（详情抽屉里有） */
const columns: DataColumn[] = [
  { key: 'username', label: '用户', minWidth: 200, mobile: 'title' },
  // 用户类型（v2.55 公益服并入用户管理）：公益服 / 付费 / 普通
  { key: 'user_type', label: '类型', width: 150 },
  { key: 'tg_bound', label: 'TG', width: 70 },
  { key: 'emby_username', label: 'Emby 账号', minWidth: 130 },
  { key: 'subscription', label: '会员', minWidth: 160 },
  // 注册渠道（v2.44.0 归因）：一眼看出这个号是哪来的
  { key: 'register_channel_label', label: '来源', width: 105 },
  { key: 'last_login_at', label: '最近登录', width: 150 },
  { key: 'created_at', label: '注册时间', width: 150, mobile: 'hide' },
  { key: 'actions', label: '操作', width: 180, fixed: 'right', align: 'right' },
]

/** 抽屉里的会员记录（抽屉在手机上接近全宽，也用卡片形态） */
const historyColumns: DataColumn[] = [
  { key: 'plan_name', label: '套餐', minWidth: 120, mobile: 'title' },
  { key: 'period', label: '有效期', minWidth: 180 },
  { key: 'days_left', label: '剩余', width: 80 },
  { key: 'status', label: '状态', width: 90 },
]

/** 对照 Jellyfin 的用户详情：把设备审查放进用户上下文，不必跳到全局风控页再搜索用户 */
const deviceColumns: DataColumn[] = [
  { key: 'name', label: '设备', minWidth: 150, mobile: 'title' },
  { key: 'client', label: '客户端', minWidth: 120 },
  { key: 'ip', label: 'IP', width: 130 },
  { key: 'last_seen_at', label: '最近活跃', width: 150 },
  { key: 'is_blocked', label: '状态', width: 90 },
  { key: 'actions', label: '操作', width: 150, fixed: 'right', align: 'right' },
]

const users = ref<AdminUserRow[]>([])
const total = ref(0)
const search = ref('')
const activeFilter = ref<string>('')
/** 用户类型筛选（v2.55 公益服并入用户管理）：空=全部，welfare=公益服，paid=付费，normal=普通 */
const typeFilter = ref<string>('')
/** 注册渠道枚举由后端下发（含「未记录」），前端不自己拼一份 */
const channelFilter = ref<string>('')
const channels = ref<Array<{ value: string; label: string }>>([])
const loading = ref(false)
/** 列表加载失败：表格空态换成可重试的错误提示，而不是「没有匹配的用户」 */
const loadError = ref(false)
const page = ref(0)
const PAGE_SIZE = 20

/** 是否有任何筛选条件（决定空态文案：没匹配 vs 还没有用户） */
const hasFilter = computed(() =>
  Boolean(search.value || activeFilter.value || channelFilter.value || typeFilter.value),
)

/** 搜索框输入防抖：停手 350ms 自动查询（回车 / 清空仍立即查询） */
let searchTimer: ReturnType<typeof setTimeout> | undefined
watch(search, () => {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(applyFilter, 350)
})
onBeforeUnmount(() => clearTimeout(searchTimer))

/**
 * 行选择 + 批量禁用/启用（v2.58）
 * - 选择是纯前端状态，筛选提交时清空；翻页保留（后端分页、每页重新勾选成本高）
 * - 自己和超管（is_staff）不可勾选：批量操作不该动到自己或超管账号
 */
const selection = useTableSelection()
/**
 * 模板 v-model 用的可写代理：`selection.selectedKeys` 是对象里的嵌套 ref，
 * 模板不会自动解包（直接绑会把 Ref 对象传给子组件导致类型错乱），包一层 computed 最稳。
 */
const selectedKeys = computed<RowKey[]>({
  get: () => selection.selectedKeys.value,
  set: (v) => { selection.selectedKeys.value = v },
})
const bulkBusy = ref(false)

/** 行可勾选规则：排除自己和超管 */
function isRowSelectable(row: AdminUserRow): boolean {
  return row.id !== auth.admin?.id && !row.is_staff
}

async function bulkToggleActive(enable: boolean) {
  const keys = selection.selectedKeys.value as number[]
  if (!keys.length) return
  if (keys.length > BULK_LIMIT) {
    ElMessage.warning(`一次最多操作 ${BULK_LIMIT} 项`)
    return
  }
  const action = enable ? '启用' : '禁用'
  try {
    await ElMessageBox.confirm(
      `确定要${action}选中的 ${keys.length} 个用户吗？`,
      `批量${action}`,
      { type: 'warning', confirmButtonText: `确认${action}`, cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  bulkBusy.value = true
  try {
    const summary = await runBulkOperation(keys, (id) => updateUser(id, { is_active: enable }))
    // 失败项保留选中态，方便直接重试；成功项已在 load() 后刷新，无需保留
    selection.selectedKeys.value = summary.failedKeys as RowKey[]
    ElMessage[summary.failed ? 'warning' : 'success'](bulkSummaryText(summary, action))
    await load()
  } finally {
    bulkBusy.value = false
  }
}

function applyFilter() {
  // 筛选提交清空选择：老选择对应的行可能已不在结果里
  selection.clear()
  page.value = 0
  load()
}

function resetFilters() {
  selection.clear()
  search.value = ''
  activeFilter.value = ''
  channelFilter.value = ''
  typeFilter.value = ''
  applyFilter()
}

/** 请求序号：筛选连续变化时只采纳最后一次请求的结果，避免旧响应覆盖新筛选 */
let loadSeq = 0

async function load() {
  const seq = ++loadSeq
  loading.value = true
  loadError.value = false
  try {
    const params: Record<string, unknown> = { limit: PAGE_SIZE, offset: page.value * PAGE_SIZE }
    if (search.value) params.search = search.value
    if (activeFilter.value !== '') params.active = activeFilter.value === 'true'
    if (channelFilter.value) params.channel = channelFilter.value
    if (typeFilter.value) params.user_type = typeFilter.value
    const res = await fetchUsers(params)
    if (seq !== loadSeq) return
    // 操作后（取消公益、禁用…）当前页可能被筛空：回退到最后一页而不是显示「没有匹配」
    if (!res.users.length && res.total > 0 && page.value > 0) {
      page.value = Math.max(0, Math.ceil(res.total / PAGE_SIZE) - 1)
      void load()
      return
    }
    users.value = res.users
    total.value = res.total
    if (res.channels?.length) channels.value = res.channels
  } catch {
    if (seq !== loadSeq) return
    /* 拦截器已提示；保留上一页数据，空态换成可重试的错误提示 */
    loadError.value = true
  } finally {
    if (seq === loadSeq) loading.value = false
  }
}

onMounted(async () => {
  await Promise.all([load(), loadPlans()])
})

function fmtDate(s: string | null): string {
  if (!s) return '—'
  return s.slice(0, 16).replace('T', ' ')
}

function fmtDay(s: string | null): string {
  return s ? s.slice(0, 10) : '—'
}

// ==================== 套餐 ====================

const plans = ref<PlanRow[]>([])

async function loadPlans() {
  try {
    plans.value = (await fetchPlans()).plans
  } catch {
    plans.value = []
  }
}

// ==================== 360° 详情 ====================

const detailVisible = ref(false)
const detailLoading = ref(false)
const detail = ref<UserDetail | null>(null)
const userDevices = ref<DeviceRow[]>([])
const deviceLoading = ref(false)
const detailTab = ref('overview')
const detailUser = ref<AdminUserRow | null>(null)
// 授权资源卡片（Phase 4）：与详情同一次交互取回，但**单独兜底**——
// 它挂了不能连累 360° 详情（面板与后端分开部署时可能短暂 404）
const grants = ref<UserGrants | null>(null)
const grantsLoading = ref(false)

async function openDetail(u: AdminUserRow) {
  detailUser.value = u
  detailTab.value = 'overview'
  detail.value = null
  grants.value = null
  detailVisible.value = true
  detailLoading.value = true
  try {
    const [userDetail, deviceResult] = await Promise.all([
      fetchUserDetail(u.id),
      fetchDevices({ user_id: u.id, limit: 100 }),
    ])
    detail.value = userDetail
    userDevices.value = deviceResult.devices
  } catch {
    // 错误提示由 HTTP 拦截器统一弹出，这里只需收尾
  } finally {
    detailLoading.value = false
  }
  grantsLoading.value = true
  fetchUserGrants(u.id)
    .then((res) => { grants.value = res })
    .catch(() => { grants.value = null })
    .finally(() => { grantsLoading.value = false })
}

/** 详情里的快捷操作后刷新两侧数据 */
async function refreshDetail() {
  if (detailUser.value) await openDetail(detailUser.value)
}

function fmtDeviceDate(value: string | null): string {
  return value ? value.slice(0, 16).replace('T', ' ') : '—'
}

async function toggleUserDevice(device: DeviceRow) {
  deviceLoading.value = true
  try {
    const nextBlocked = !device.is_blocked
    const res = await setDeviceBlocked(device.user_id, device.device_id, nextBlocked)
    ElMessage.success(res.message)
    userDevices.value = (await fetchDevices({ user_id: device.user_id, limit: 100 })).devices
  } catch {
    // 拦截器已提示
  } finally {
    deviceLoading.value = false
  }
}

async function removeUserDevice(device: DeviceRow) {
  try {
    await ElMessageBox.confirm(
      `移除「${device.name || device.device_id}」后客户端需要重新登录，确定吗？`,
      '移除设备',
      { type: 'warning' },
    )
  } catch {
    return // 取消
  }
  deviceLoading.value = true
  try {
    const res = await removeDevice(device.user_id, device.device_id)
    ElMessage.success(res.message)
    userDevices.value = (await fetchDevices({ user_id: device.user_id, limit: 100 })).devices
  } catch {
    // 拦截器已提示
  } finally {
    deviceLoading.value = false
  }
}

// ==================== 授予 / 延长会员 ====================

const subDialog = reactive({
  visible: false,
  mode: 'grant' as 'grant' | 'extend',
  user: null as AdminUserRow | null,
  planId: undefined as number | undefined,
  days: 30,
})

const grantPreview = computed(() => {
  const u = subDialog.user
  if (!u) return ''
  const base = u.has_subscription && u.subscription_end ? new Date(u.subscription_end) : new Date()
  const from = base.getTime() > Date.now() ? base : new Date()
  const end = new Date(from.getTime() + subDialog.days * 86400000)
  return `${from.toISOString().slice(0, 10)} → ${end.toISOString().slice(0, 10)}`
})

function openGrant(u: AdminUserRow) {
  subDialog.mode = 'grant'
  subDialog.user = u
  subDialog.planId = plans.value[0]?.id
  subDialog.days = plans.value[0]?.duration_days ?? 30
  subDialog.visible = true
}

function openExtend(u: AdminUserRow) {
  subDialog.mode = 'extend'
  subDialog.user = u
  subDialog.days = 30
  subDialog.visible = true
}

function onPlanChange(id: number | undefined) {
  const plan = plans.value.find((p) => p.id === id)
  if (plan) subDialog.days = plan.duration_days
}

const subSaving = ref(false)

async function submitSub() {
  const u = subDialog.user
  if (!u) return
  subSaving.value = true
  try {
    if (subDialog.mode === 'grant') {
      if (!subDialog.planId) {
        ElMessage.warning('请选择套餐')
        return
      }
      await grantSubscription(u.id, { plan_id: subDialog.planId, duration_days: subDialog.days })
      ElMessage.success('会员已授予并通知用户')
    } else {
      if (!u.subscription_id) return
      await extendSubscription(u.subscription_id, subDialog.days)
      ElMessage.success('会员已延长并通知用户')
    }
    subDialog.visible = false
    await load()
    if (detailVisible.value) await refreshDetail()
  } catch {
    // 拦截器已提示
  } finally {
    subSaving.value = false
  }
}

// ==================== 公益服操作（v2.55 公益用户页并入） ====================

const welfareGrantDlg = reactive({
  visible: false,
  user: null as AdminUserRow | null,
  days: 30,
  busy: false,
})

function openWelfareGrant(row: AdminUserRow) {
  welfareGrantDlg.user = row
  welfareGrantDlg.days = 30
  welfareGrantDlg.visible = true
}

async function submitWelfareGrant() {
  const u = welfareGrantDlg.user
  if (!u) return
  welfareGrantDlg.busy = true
  try {
    await grantWelfare({ user_id: u.id, days: welfareGrantDlg.days, channel: 'admin' })
    ElMessage.success('已开通/续期公益')
    welfareGrantDlg.visible = false
    await load()
  } catch {
    // 拦截器已提示
  } finally {
    welfareGrantDlg.busy = false
  }
}

async function doWelfareRevoke(row: AdminUserRow) {
  try {
    await ElMessageBox.confirm(`确定取消 ${row.username} 的公益资格？`, '确认', { type: 'warning' })
  } catch {
    return
  }
  try {
    await revokeWelfare(row.id)
    ElMessage.success('已取消公益资格')
    await load()
  } catch {
    // 拦截器已提示
  }
}

const welfareBulkDlg = reactive({
  visible: false,
  min_expired_days: 0,
  max_expired_days: 30,
  add_days: 30,
  busy: false,
})

async function submitWelfareBulk() {
  welfareBulkDlg.busy = true
  try {
    const res = await bulkExtendWelfare({
      min_expired_days: welfareBulkDlg.min_expired_days,
      max_expired_days: welfareBulkDlg.max_expired_days,
      add_days: welfareBulkDlg.add_days,
    })
    ElMessage.success(`已延期 ${res.affected} 个用户`)
    welfareBulkDlg.visible = false
    await load()
  } catch {
    // 拦截器已提示
  } finally {
    welfareBulkDlg.busy = false
  }
}

function fmtWelfareDate(s: string | null): string {
  if (!s) return '永不过期'
  return s.replace('T', ' ').slice(0, 16)
}

// ==================== 积分调整 ====================

const pointsDialog = reactive({
  visible: false,
  user: null as AdminUserRow | null,
  amount: 0,
  reason: '',
})
const pointsSaving = ref(false)

function openPoints(u: AdminUserRow) {
  pointsDialog.user = u
  pointsDialog.amount = 0
  pointsDialog.reason = ''
  pointsDialog.visible = true
}

async function submitPoints() {
  const u = pointsDialog.user
  if (!u) return
  if (!pointsDialog.amount) {
    ElMessage.warning('请输入不为 0 的调整数额')
    return
  }
  pointsSaving.value = true
  try {
    const res = await adjustUserPoints(u.id, {
      amount: pointsDialog.amount,
      reason: pointsDialog.reason || '管理员人工调整',
    })
    ElMessage.success(`已调整，当前余额 ${res.balance}`)
    pointsDialog.visible = false
    if (detailVisible.value) await refreshDetail()
  } catch {
    // 拦截器已提示
  } finally {
    pointsSaving.value = false
  }
}

// ==================== 其他操作 ====================

async function toggleActive(u: AdminUserRow) {
  const action = u.is_active ? '禁用' : '启用'
  try {
    await ElMessageBox.confirm(`确定要${action}用户「${u.username}」吗？`, '确认', { type: 'warning' })
  } catch {
    return // 取消
  }
  try {
    await updateUser(u.id, { is_active: !u.is_active })
    ElMessage.success(`已${action}`)
    load()
  } catch {
    // 拦截器已提示
  }
}

async function toggleStaff(u: AdminUserRow) {
  if (u.id === auth.admin?.id) {
    ElMessage.warning('不能修改自己的权限')
    return
  }
  const action = u.is_staff ? '移除管理员' : '设为管理员'
  try {
    await ElMessageBox.confirm(`确定要${action}「${u.username}」吗？`, '确认', { type: 'warning' })
  } catch {
    return // 取消
  }
  try {
    await updateUser(u.id, { is_staff: !u.is_staff })
    ElMessage.success('已更新')
    load()
  } catch {
    // 拦截器已提示
  }
}

const pwdDialog = reactive({ visible: false, user: null as AdminUserRow | null, value: '' })
const pwdSaving = ref(false)

function openPwd(u: AdminUserRow) {
  pwdDialog.user = u
  pwdDialog.value = ''
  pwdDialog.visible = true
}

async function submitPwd() {
  const u = pwdDialog.user
  if (!u) return
  if (pwdDialog.value.length < 6) {
    ElMessage.warning('密码长度需为 6-64 位')
    return
  }
  pwdSaving.value = true
  try {
    await resetUserPassword(u.id, pwdDialog.value)
    ElMessage.success('密码已重置并同步 Emby')
    pwdDialog.visible = false
  } catch {
    // 拦截器已提示
  } finally {
    pwdSaving.value = false
  }
}

const msgDialog = reactive({ visible: false, user: null as AdminUserRow | null, title: '', content: '' })
const msgSaving = ref(false)

function openMsg(u: AdminUserRow) {
  msgDialog.user = u
  msgDialog.title = '管理员消息'
  msgDialog.content = ''
  msgDialog.visible = true
}

async function submitMsg() {
  const u = msgDialog.user
  if (!u || !msgDialog.content.trim()) {
    ElMessage.warning('请输入消息内容')
    return
  }
  msgSaving.value = true
  try {
    await sendUserMessage(u.id, { title: msgDialog.title || '管理员消息', content: msgDialog.content })
    ElMessage.success('消息已发送')
    msgDialog.visible = false
  } catch {
    // 拦截器已提示
  } finally {
    msgSaving.value = false
  }
}

const broadcastVisible = ref(false)
const broadcastForm = reactive({ title: '系统广播', content: '' })
const broadcastSaving = ref(false)

async function submitBroadcast() {
  if (!broadcastForm.content.trim()) {
    ElMessage.warning('请输入广播内容')
    return
  }
  broadcastSaving.value = true
  try {
    await broadcastMessage({ title: broadcastForm.title || '系统广播', content: broadcastForm.content })
    ElMessage.success('广播已发送给全部用户')
    broadcastVisible.value = false
    broadcastForm.content = ''
  } catch {
    // 拦截器已提示；保留已输入的内容
  } finally {
    broadcastSaving.value = false
  }
}

function onRowCommand(cmd: string, row: AdminUserRow) {
  const map: Record<string, () => void> = {
    detail: () => openDetail(row),
    grant: () => openGrant(row),
    extend: () => openExtend(row),
    points: () => openPoints(row),
    welfare_grant: () => openWelfareGrant(row),
    welfare_revoke: () => doWelfareRevoke(row),
    password: () => openPwd(row),
    message: () => openMsg(row),
    active: () => toggleActive(row),
    staff: () => toggleStaff(row),
  }
  map[cmd]?.()
}

const LOGIN_LIMIT = 8
function logTypeLabel(type: string): string {
  const map: Record<string, string> = {
    checkin: '签到', rebate: '返利', recharge: '充值', exchange: '兑换',
    subscribe: '订阅', admin: '人工调整', order: '订单', refund: '退款',
  }
  return map[type] || type
}

// ==================== 授权资源卡片（Phase 4） ====================

/** 卡片状态点：生效 / 即将到期 / 未授权（颜色只在点上，卡片本体不染色） */
const BADGE_BY_STATE: Record<string, string> = { ok: 'au-badge-green', warn: 'au-badge-amber', off: 'au-badge-muted' }
function grantState(card: UserGrantCard): { cls: string; text: string } {
  if (card.state === 'warn') return { cls: 'warn', text: `${card.subscription?.days_left ?? 0} 天后到期` }
  if (card.state === 'ok') return { cls: 'ok', text: card.grant_label }
  return { cls: 'off', text: card.grant_label }
}

/** 授权来源的一句话：管理员不展开卡片也能知道「凭什么能看」 */
function grantReason(card: UserGrantCard): string {
  const sub = card.subscription
  if (card.grant === 'subscription') {
    return `${sub?.plan_name || '会员'} · ${fmtDay(sub?.end_date ?? null)} 到期`
  }
  if (card.grant === 'expired') {
    if (sub?.cancelled) return `${sub.plan_name || '会员'} 已退款取消`
    return `${sub?.plan_name || '会员'} 已于 ${fmtDay(sub?.end_date ?? null)} 到期`
  }
  if (card.grant === 'unlock') {
    const u = card.unlock
    return u?.expires_at ? `积分解锁 · ${fmtDay(u.expires_at)} 到期` : '积分解锁 · 永久有效'
  }
  if (card.grant === 'free_open') return '公益服免费开放，未解锁 Emby 账号查看'
  return '付费服未开通会员，播放会被付费墙拦下'
}

/** 资源数字：条目数常在十万级，给个千分位比堆 6 位数字好读；null 显示「—」而不是 0 */
function fmtCount(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—'
  return n.toLocaleString('zh-CN')
}

</script>

<template>
  <div class="admin-page users-page">
    <PageHeader
      eyebrow="用户与账号"
      title="用户"
      description="点用户名或「详情」打开 360° 画像：会员、积分、订单、邀请、设备与授权资源都在抽屉里处理。"
    >
      <template #actions>
        <el-button @click="broadcastVisible = true">
          <Megaphone :size="14" class="btn-ico" />全站广播
        </el-button>
        <el-button :loading="loading" @click="load">
          <RefreshCw :size="14" class="btn-ico" />刷新
        </el-button>
        <el-button @click="welfareBulkDlg.visible = true">
          <CalendarCheck :size="14" class="btn-ico" />公益批量延期
        </el-button>
      </template>
    </PageHeader>

    <SectionCard title="全部用户" :icon="UsersIcon" :meta="`共 ${total} 位`" flush>
      <!-- 工具栏：筛选在左，重置在右 -->
      <div class="users-toolbar">
        <div class="users-filters">
          <el-input
            v-model="search"
            class="f-search"
            placeholder="搜索用户名 / 邮箱"
            aria-label="搜索用户"
            clearable
            @keyup.enter="applyFilter"
            @clear="applyFilter"
          >
            <template #prefix><Search :size="14" /></template>
          </el-input>
          <el-select v-model="activeFilter" class="f-select" placeholder="账号状态" clearable @change="applyFilter">
            <el-option label="正常" value="true" />
            <el-option label="已禁用" value="false" />
          </el-select>
          <el-select v-model="channelFilter" class="f-select" placeholder="注册来源" clearable @change="applyFilter">
            <el-option v-for="c in channels" :key="c.value" :label="c.label" :value="c.value" />
          </el-select>
          <el-select v-model="typeFilter" class="f-select" placeholder="用户类型" clearable @change="applyFilter">
            <el-option label="公益服" value="welfare" />
            <el-option label="付费（会员生效中）" value="paid" />
            <el-option label="普通（无会员）" value="normal" />
          </el-select>
        </div>
        <div v-if="hasFilter" class="users-actions">
          <el-button text @click="resetFilters">清除筛选</el-button>
        </div>
      </div>

      <DataTable :rows="users" :columns="columns" :loading="loading"
        empty="暂无用户"
        empty-description="用户注册后会出现在这里。"
        :error="loadError ? '可能是网络或后端暂时不可用。' : ''"
        :has-active-filters="hasFilter"
        :pagination="{ total, page: page + 1, pageSize: PAGE_SIZE }"
        selectable
        v-model:selectedKeys="selectedKeys"
        :is-row-selectable="isRowSelectable"
        @page-change="(p: number) => { page = p - 1; load() }"
        @retry="load"
        @clear-filters="resetFilters">
        <template #bulk-bar>
          <el-button type="warning" plain size="small" :loading="bulkBusy" @click="bulkToggleActive(false)">批量禁用</el-button>
          <el-button type="success" plain size="small" :loading="bulkBusy" @click="bulkToggleActive(true)">批量启用</el-button>
        </template>

        <template #cell-username="{ row }">
          <div class="user-cell">
            <button class="user-link" @click="openDetail(row)">{{ row.username }}</button>
            <!-- 角色位始终有值：非管理员的用户不再是一片空白（v2.42.5） -->
            <span class="au-badge" :class="row.is_staff ? 'au-badge-amber' : 'au-badge-muted'">
              {{ row.is_staff ? '管理员' : '用户' }}
            </span>
            <span v-if="!row.is_active" class="au-badge au-badge-rose">已禁用</span>
          </div>
          <div class="user-sub">{{ row.email || '未绑定邮箱' }}</div>
        </template>

        <template #cell-emby_username="{ row }">{{ row.emby_username || '—' }}</template>

        <template #cell-subscription="{ row }">
          <template v-if="row.has_subscription">
            <span class="au-badge au-badge-green">生效中</span>
            <div class="user-sub">至 {{ fmtDay(row.subscription_end) }}</div>
          </template>
          <span v-else class="au-badge au-badge-muted">无会员</span>
        </template>

        <template #cell-user_type="{ row }">
          <template v-if="row.user_type === 'welfare'">
            <span class="au-badge au-badge-green">公益服</span>
            <div class="user-sub">
              <span
                v-if="row.welfare_expires_at && new Date(row.welfare_expires_at) < new Date()"
                class="welfare-expired"
              >已过期</span>
              <span v-else>{{ fmtWelfareDate(row.welfare_expires_at) }}</span>
            </div>
          </template>
          <span v-else-if="row.user_type === 'paid'" class="au-badge au-badge-amber">付费</span>
          <span v-else class="au-badge au-badge-muted">普通</span>
        </template>

        <template #cell-tg_bound="{ row }">
          <span v-if="row.tg_bound" class="au-badge au-badge-info" title="已绑定 Telegram">已绑</span>
          <span v-else class="au-badge au-badge-muted" title="未绑定 Telegram">未绑</span>
        </template>

        <template #cell-last_login_at="{ row }"><span class="au-num">{{ fmtDate(row.last_login_at) }}</span></template>

        <template #cell-created_at="{ row }"><span class="au-num">{{ fmtDate(row.created_at) }}</span></template>

        <template #cell-actions="{ row }">
          <el-button size="small" text type="primary" @click="openDetail(row)">
            <Eye :size="14" class="btn-ico-sm" />详情
          </el-button>
          <el-dropdown trigger="click" @command="(cmd: string) => onRowCommand(cmd, row)">
            <el-button size="small" text aria-label="更多操作">
              <MoreHorizontal :size="16" />
            </el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="grant">授予会员</el-dropdown-item>
                <el-dropdown-item v-if="row.subscription_id" command="extend">延长会员</el-dropdown-item>
                <el-dropdown-item command="points">调整积分</el-dropdown-item>
                <el-dropdown-item command="welfare_grant">公益开通/续期</el-dropdown-item>
                <el-dropdown-item v-if="row.is_welfare" command="welfare_revoke" divided>取消公益资格</el-dropdown-item>
                <el-dropdown-item command="password" divided>重置密码</el-dropdown-item>
                <el-dropdown-item command="message">发送消息</el-dropdown-item>
                <el-dropdown-item command="active" divided>
                  {{ row.is_active ? '禁用账号' : '启用账号' }}
                </el-dropdown-item>
                <el-dropdown-item command="staff">
                  {{ row.is_staff ? '移除管理员' : '设为管理员' }}
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </template>
      </DataTable>
    </SectionCard>

    <!-- ==================== 用户 360° 详情 ==================== -->
    <el-drawer
      v-model="detailVisible"
      :size="drawerSize"
      :title="detailUser ? `用户画像 · ${detailUser.username}` : '用户详情'"
      class="user-drawer"
    >
      <div v-loading="detailLoading" class="detail-body">
        <EmptyState
          v-if="!detailLoading && !detail"
          :icon="UserX"
          title="用户详情读取失败"
          description="可能是网络或后端暂时不可用。"
        >
          <template #actions><el-button size="small" @click="refreshDetail">重试</el-button></template>
        </EmptyState>

        <template v-if="detail">
          <!-- 概览 -->
          <div class="detail-hero">
            <div class="hero-avatar" aria-hidden="true">{{ detail.profile.username.charAt(0).toUpperCase() }}</div>
            <div class="hero-main">
              <div class="hero-name">
                <span class="au-serif">{{ detail.profile.username }}</span>
                <span v-if="detail.profile.is_staff" class="au-badge au-badge-amber">管理员</span>
                <span v-if="!detail.profile.is_active" class="au-badge au-badge-rose">已禁用</span>
              </div>
              <div class="hero-sub">
                ID {{ detail.profile.id }} · {{ detail.profile.email || '未绑定邮箱' }} ·
                Emby {{ detail.profile.emby_username || '—' }}
              </div>
              <div class="hero-sub">注册 {{ fmtDate(detail.profile.created_at) }} · 最近登录 {{ fmtDate(detail.profile.last_login_at) }}</div>
            </div>
          </div>

          <div class="detail-stats">
            <StatTile
              label="会员"
              :icon="Crown"
              :value="detail.subscription.active ? detail.subscription.active.plan_name : '无会员'"
              :suffix="detail.subscription.active ? `剩 ${detail.subscription.active.days_left} 天` : ''"
              class="text-tile"
            />
            <StatTile label="积分" :icon="Coins" :value="detail.points.balance" />
            <StatTile
              label="签到"
              :icon="CalendarCheck"
              :value="detail.checkin.total"
              :suffix="`次 · 连签 ${detail.checkin.streak}`"
            />
            <StatTile
              label="邀请"
              :icon="Gift"
              :value="detail.invitation.count"
              :suffix="`人 · 返利 ${detail.invitation.rebate_total}`"
            />
            <StatTile label="累计付费" :icon="Wallet" :value="`¥${detail.orders.paid_total.toFixed(2)}`" />
            <StatTile
              label="观看"
              :icon="Film"
              :value="detail.watch.plays"
              :suffix="`次 · 已看 ${detail.watch.watched_items}`"
            />
          </div>

          <div class="detail-actions">
            <el-button size="small" type="primary" @click="detailUser && openGrant(detailUser)">
              <Crown :size="13" class="btn-ico" />授予会员
            </el-button>
            <el-button v-if="detailUser?.subscription_id" size="small" @click="detailUser && openExtend(detailUser)">延长会员</el-button>
            <el-button size="small" @click="detailUser && openPoints(detailUser)">
              <Coins :size="13" class="btn-ico" />调整积分
            </el-button>
            <el-button size="small" @click="detailUser && openPwd(detailUser)">
              <KeyRound :size="13" class="btn-ico" />重置密码
            </el-button>
            <el-button size="small" @click="detailUser && openMsg(detailUser)">
              <Mail :size="13" class="btn-ico" />发送消息
            </el-button>
            <el-button size="small" @click="detailUser && toggleStaff(detailUser)">
              <ShieldCheck :size="13" class="btn-ico" />{{ detailUser?.is_staff ? '移除管理员' : '设为管理员' }}
            </el-button>
          </div>

          <el-tabs v-model="detailTab" class="detail-tabs">
            <!-- 授权资源：一个服一张卡（Phase 4） -->
            <el-tab-pane label="授权资源" name="grants">
              <template #label>
                <span class="tab-label"><Server :size="13" />授权资源</span>
              </template>
              <div v-if="grantsLoading" class="grant-skeleton" aria-busy="true" aria-label="授权卡片读取中">
                <div class="au-skeleton sk-row" />
                <div class="au-skeleton sk-card" />
              </div>
              <EmptyState
                v-else-if="!grants"
                compact
                :icon="Server"
                title="授权卡片读取失败"
                description="不影响上面的资料与操作；关掉抽屉重开可重试。"
              />
              <template v-else>
                <div class="grant-summary">
                  <div class="gs-item">
                    <span class="gs-label">可播放</span>
                    <span class="gs-value">
                      {{ grants.summary.realms_playable }} / {{ grants.summary.realms_total }} 个服
                    </span>
                  </div>
                  <div class="gs-item" :class="{ 'is-warn': grants.summary.realms_expiring > 0 }">
                    <span class="gs-label">即将到期</span>
                    <span class="gs-value">
                      {{ grants.summary.realms_expiring }} 个服
                    </span>
                  </div>
                  <div class="gs-item">
                    <span class="gs-label">已过期</span>
                    <span class="gs-value">{{ grants.summary.realms_expired }} 个服</span>
                  </div>
                  <div class="gs-item">
                    <span class="gs-label">活跃设备</span>
                    <span class="gs-value">
                      {{ grants.summary.devices_used === null ? '—' : `${grants.summary.devices_used} 台` }}
                      <em v-if="grants.summary.device_limit !== null">
                        {{ grants.summary.device_limit ? `/ 上限 ${grants.summary.device_limit}` : '（不限）' }}
                      </em>
                    </span>
                  </div>
                  <div class="gs-item">
                    <span class="gs-label">播放线路</span>
                    <span class="gs-value">{{ grants.summary.play_line_label }}</span>
                  </div>
                </div>

                <EmptyState
                  v-if="grants.cards.length === 0"
                  compact
                  :icon="Server"
                  title="还没有配置任何服"
                  description="先到「服管理」建一个。"
                />
                <div v-else class="grant-grid">
                  <div v-for="card in grants.cards" :key="card.realm_id" class="grant-card">
                    <div class="grant-head">
                      <span class="grant-dot" :class="grantState(card).cls" />
                      <b class="grant-name">{{ card.realm_name }}</b>
                      <span class="au-badge" :class="BADGE_BY_STATE[grantState(card).cls] || 'au-badge-muted'">{{ grantState(card).text }}</span>
                      <span v-if="card.is_default" class="au-badge au-badge-muted">默认服</span>
                      <span v-if="card.is_free" class="au-badge au-badge-info">公益服</span>
                      <span v-if="!card.is_active" class="au-badge au-badge-rose">已停用</span>
                    </div>

                    <p class="grant-reason">{{ grantReason(card) }}</p>

                    <div class="grant-caps">
                      <span class="cap" :class="card.can_play ? 'on' : 'off'">
                        <PlayCircle :size="12" />{{ card.can_play ? '可播放' : '不可播放' }}
                      </span>
                      <span class="cap" :class="card.view_granted ? 'on' : 'off'">
                        <component :is="card.view_granted ? Eye : EyeOff" :size="12" />
                        {{ card.view_granted ? '可见账号' : '账号未下发' }}
                      </span>
                      <span class="cap" :class="card.download_allowed ? 'on' : 'off'">
                        <Download :size="12" />{{ card.download_allowed ? '可下载' : '禁下载' }}
                      </span>
                      <span v-if="card.subscription?.auto_renew" class="cap on">
                        <CircleCheck :size="12" />自动续订（能量）
                      </span>
                    </div>

                    <div class="grant-metrics">
                      <div class="grant-metric">
                        <b>
                          {{ fmtCount(card.resources.enabled_libraries) }}<em v-if="card.resources.libraries !== null">/{{ card.resources.libraries }}</em>
                        </b>
                        <span>媒体库</span>
                      </div>
                      <div class="grant-metric">
                        <b>{{ fmtCount(card.resources.items) }}</b>
                        <span>条目</span>
                      </div>
                      <div class="grant-metric">
                        <b>
                          {{ fmtCount(card.resources.nodes_online) }}<em v-if="card.resources.nodes !== null">/{{ card.resources.nodes }}</em>
                        </b>
                        <span>出流节点</span>
                      </div>
                    </div>

                    <p v-if="card.is_free && card.access_note" class="grant-note">
                      <Waypoints :size="12" />{{ card.access_note }}
                    </p>
                    <p v-else-if="!card.can_play" class="grant-note muted">
                      <CircleSlash :size="12" />到「订阅套餐」授予该服会员，或用该服的会员卡码开通
                    </p>
                    <p v-else-if="card.is_free && !card.unlock?.unlocked" class="grant-note">
                      <MonitorSmartphone :size="12" />能看全库，但客户端需在个人中心花积分解锁账号地址
                    </p>
                  </div>
                </div>

                <p class="grant-scope">{{ grants.summary.scope_note }}</p>
              </template>
            </el-tab-pane>

            <el-tab-pane label="订阅记录" name="overview">
              <EmptyState
                v-if="detail.subscription.history.length === 0"
                compact
                :icon="History"
                title="暂无订阅记录"
                description="授予或用户自己购买订阅后会出现在这里。"
              />
              <DataTable
                v-else
                :rows="detail.subscription.history"
                :columns="historyColumns"
                empty="暂无订阅记录"
              >
                <template #cell-plan_name="{ row }">{{ row.plan_name }}</template>

                <template #cell-period="{ row }">
                  {{ fmtDay(row.start_date) }} → {{ fmtDay(row.end_date) }}
                </template>

                <template #cell-days_left="{ row }">{{ row.days_left }} 天</template>

                <template #cell-status="{ row }">
                  <span class="au-badge" :class="row.status === 'active' && row.days_left > 0 ? 'au-badge-green' : 'au-badge-muted'">
                    {{ row.status === 'active' && row.days_left > 0 ? '生效中' : '已结束' }}
                  </span>
                </template>
              </DataTable>
            </el-tab-pane>

            <el-tab-pane label="积分流水" name="points">
              <div class="mini-summary">
                累计获得 <strong class="ok">+{{ detail.points.income }}</strong>
                · 累计消耗 <strong class="bad">-{{ detail.points.expense }}</strong>
                · 当前 <strong>{{ detail.points.balance }}</strong>
              </div>
              <EmptyState v-if="detail.points.recent.length === 0" compact :icon="Coins" title="暂无积分流水" />
              <div v-for="l in detail.points.recent.slice(0, LOGIN_LIMIT)" :key="l.id" class="line-row">
                <span class="au-badge au-badge-amber line-tag">{{ logTypeLabel(l.type) }}</span>
                <span class="line-desc">{{ l.description || '—' }}</span>
                <span class="line-amount" :class="l.amount >= 0 ? 'ok' : 'bad'">
                  {{ l.amount >= 0 ? '+' : '' }}{{ l.amount }}
                </span>
                <span class="line-date">{{ fmtDate(l.created_at) }}</span>
              </div>
            </el-tab-pane>

            <el-tab-pane label="订单" name="orders">
              <EmptyState
                v-if="!detail.orders.recharge.length && !detail.orders.subscription.length"
                compact
                :icon="Wallet"
                title="暂无订单"
              />
              <div v-for="o in detail.orders.recharge" :key="o.order_id" class="line-row">
                <span class="au-badge au-badge-amber line-tag">充值</span>
                <span class="line-desc">{{ o.item_name }} · {{ o.points }} 积分</span>
                <span class="line-amount">¥{{ o.amount }}</span>
                <span class="line-date">{{ fmtDate(o.created_at) }}</span>
              </div>
              <div v-for="o in detail.orders.subscription" :key="o.order_id" class="line-row">
                <span class="au-badge au-badge-amber line-tag">订阅</span>
                <span class="line-desc">{{ o.item_name }}</span>
                <span class="line-amount">¥{{ o.amount }}</span>
                <span class="line-date">{{ fmtDate(o.created_at) }}</span>
              </div>
            </el-tab-pane>

            <el-tab-pane label="邀请" name="invite">
              <EmptyState v-if="detail.invitation.invitees.length === 0" compact :icon="Gift" title="暂无邀请记录" />
              <div v-for="(i, idx) in detail.invitation.invitees" :key="idx" class="line-row">
                <span class="au-badge au-badge-amber line-tag">邀请</span>
                <span class="line-desc">{{ i.username }}</span>
                <span class="line-amount ok">+{{ i.reward_points }}</span>
                <span class="line-date">{{ fmtDate(i.created_at) }}</span>
              </div>
            </el-tab-pane>

            <el-tab-pane label="设备" name="devices">
              <div class="mini-summary">共 {{ userDevices.length }} 台设备 · 可直接禁用异常设备或踢下线</div>
              <DataTable
                :rows="userDevices"
                :columns="deviceColumns"
                :loading="deviceLoading || detailLoading"
                row-key="device_id"
                empty="该用户还没有登录设备"
              >
                <template #empty>
                  <EmptyState compact :icon="Smartphone" title="该用户还没有登录设备" />
                </template>
                <template #cell-name="{ row }">{{ row.name || row.device_id }}</template>
                <template #cell-client="{ row }">{{ row.client || '—' }}</template>
                <template #cell-ip="{ row }"><span class="mono">{{ row.ip || '—' }}</span></template>
                <template #cell-last_seen_at="{ row }">{{ fmtDeviceDate(row.last_seen_at) }}</template>
                <template #cell-is_blocked="{ row }">
                  <span class="au-badge" :class="row.is_blocked ? 'au-badge-rose' : 'au-badge-green'">{{ row.is_blocked ? '已禁用' : '正常' }}</span>
                </template>
                <template #cell-actions="{ row }">
                  <el-button size="small" text :type="row.is_blocked ? 'success' : 'warning'" @click="toggleUserDevice(row)">
                    {{ row.is_blocked ? '启用' : '禁用' }}
                  </el-button>
                  <el-button size="small" text type="danger" @click="removeUserDevice(row)">踢下线</el-button>
                </template>
              </DataTable>
            </el-tab-pane>
          </el-tabs>
        </template>
      </div>
    </el-drawer>

    <!-- ==================== 授予 / 延长会员 ==================== -->
    <el-dialog
      v-model="subDialog.visible"
      :title="subDialog.mode === 'grant' ? '授予会员' : '延长会员'"
      width="min(440px, 92vw)"
    >
      <el-form label-width="80px">
        <el-form-item label="用户">
          <span class="dialog-user">{{ subDialog.user?.username }}</span>
        </el-form-item>
        <el-form-item v-if="subDialog.mode === 'grant'" label="套餐">
          <el-select v-model="subDialog.planId" class="w-full" @change="onPlanChange">
            <el-option
              v-for="p in plans"
              :key="p.id"
              :label="`${p.name}（${p.duration_days} 天 / ¥${p.price}）`"
              :value="p.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item v-else label="当前到期">
          <span class="dialog-user">{{ fmtDay(subDialog.user?.subscription_end ?? null) }}</span>
        </el-form-item>
        <el-form-item label="天数">
          <el-input-number v-model="subDialog.days" :min="1" :max="3650" class="w-full" />
        </el-form-item>
        <el-form-item label="预期区间">
          <span class="dialog-hint">{{ grantPreview }}</span>
        </el-form-item>
        <el-form-item v-if="plans.length === 0 && subDialog.mode === 'grant'" label=" ">
          <span class="dialog-hint warn">还没有可授予的套餐，先去「订阅套餐」页创建一个，回来再选</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="subDialog.visible = false">取消</el-button>
        <el-button type="primary" :loading="subSaving" @click="submitSub">确认</el-button>
      </template>
    </el-dialog>

    <!-- ==================== 积分调整 ==================== -->
    <el-dialog v-model="pointsDialog.visible" title="调整积分" width="min(420px, 92vw)">
      <el-form label-width="80px">
        <el-form-item label="用户">
          <span class="dialog-user">{{ pointsDialog.user?.username }}</span>
        </el-form-item>
        <el-form-item label="调整数额">
          <el-input-number v-model="pointsDialog.amount" :min="-1000000" :max="1000000" class="w-full" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="pointsDialog.reason" placeholder="例如：活动补偿 / 客服补偿" />
        </el-form-item>
        <el-form-item label=" ">
          <span class="dialog-hint">正数为发放，负数为扣减；操作会写入积分台账与管理员日志</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="pointsDialog.visible = false">取消</el-button>
        <el-button type="primary" :loading="pointsSaving" @click="submitPoints">确认调整</el-button>
      </template>
    </el-dialog>

    <!-- ==================== 重置密码 ==================== -->
    <el-dialog v-model="pwdDialog.visible" title="重置密码" width="min(400px, 92vw)">
      <el-form label-width="80px">
        <el-form-item label="用户">
          <span class="dialog-user">{{ pwdDialog.user?.username }}</span>
        </el-form-item>
        <el-form-item label="新密码">
          <el-input v-model="pwdDialog.value" type="password" show-password placeholder="6-64 位，将同步为 Emby 密码" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="pwdDialog.visible = false">取消</el-button>
        <el-button type="primary" :loading="pwdSaving" @click="submitPwd">确认重置</el-button>
      </template>
    </el-dialog>

    <!-- ==================== 公益开通/续期 ==================== -->
    <el-dialog v-model="welfareGrantDlg.visible" title="开通/续期公益" width="min(400px, 92vw)">
      <el-form label-width="80px">
        <el-form-item label="用户">
          <span class="dialog-user">{{ welfareGrantDlg.user?.username }}</span>
        </el-form-item>
        <el-form-item label="天数">
          <el-input-number v-model="welfareGrantDlg.days" :min="0" :max="3650" />
          <span class="au-hint">0 = 永不过期</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="welfareGrantDlg.visible = false">取消</el-button>
        <el-button type="primary" :loading="welfareGrantDlg.busy" @click="submitWelfareGrant">确定</el-button>
      </template>
    </el-dialog>

    <!-- ==================== 公益批量延期 ==================== -->
    <el-dialog v-model="welfareBulkDlg.visible" title="批量延期公益" width="min(420px, 92vw)">
      <el-form label-width="110px">
        <el-form-item label="过期天数范围">
          <div class="range-row">
            <el-input-number v-model="welfareBulkDlg.min_expired_days" :min="0" class="range-num" />
            <span class="range-sep">~</span>
            <el-input-number v-model="welfareBulkDlg.max_expired_days" :min="welfareBulkDlg.min_expired_days" class="range-num" />
          </div>
          <span class="dialog-hint">已过期天数落在该区间内的公益用户会统一延期</span>
        </el-form-item>
        <el-form-item label="增加天数">
          <el-input-number v-model="welfareBulkDlg.add_days" :min="1" :max="365" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="welfareBulkDlg.visible = false">取消</el-button>
        <el-button type="primary" :loading="welfareBulkDlg.busy" @click="submitWelfareBulk">确定</el-button>
      </template>
    </el-dialog>

    <!-- ==================== 发送消息 ==================== -->
    <el-dialog v-model="msgDialog.visible" title="发送站内消息" width="min(460px, 92vw)">
      <el-form label-width="80px">
        <el-form-item label="收件人">
          <span class="dialog-user">{{ msgDialog.user?.username }}</span>
        </el-form-item>
        <el-form-item label="标题">
          <el-input v-model="msgDialog.title" />
        </el-form-item>
        <el-form-item label="内容">
          <el-input v-model="msgDialog.content" type="textarea" :rows="4" placeholder="输入消息内容…" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="msgDialog.visible = false">取消</el-button>
        <el-button type="primary" :loading="msgSaving" @click="submitMsg">发送</el-button>
      </template>
    </el-dialog>

    <!-- ==================== 全站广播 ==================== -->
    <el-dialog v-model="broadcastVisible" title="全站广播" width="min(460px, 92vw)">
      <el-form label-width="80px">
        <el-form-item label="标题">
          <el-input v-model="broadcastForm.title" />
        </el-form-item>
        <el-form-item label="内容">
          <el-input v-model="broadcastForm.content" type="textarea" :rows="4" placeholder="将推送给全部用户…" />
        </el-form-item>
        <el-form-item label=" ">
          <span class="dialog-hint warn">会推送给全部用户，发出后无法撤回。</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="broadcastVisible = false">取消</el-button>
        <el-button type="primary" :loading="broadcastSaving" @click="submitBroadcast">发送广播</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.users-page { gap: 16px; }
.btn-ico { margin-right: 4px; }
.btn-ico-sm { margin-right: 2px; }
.w-full { width: 100%; }

/* ===== 工具栏：筛选左、操作右 ===== */
.users-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
  padding: 12px 20px;
  border-bottom: 1px solid var(--au-border);
}
.users-filters { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; flex: 1 1 auto; min-width: 0; }
.users-actions { display: flex; align-items: center; gap: 8px; }
.f-search { width: 240px; }
.f-select { width: 150px; }

/* ===== 列表单元格 ===== */
.user-cell { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.user-link {
  background: none;
  border: none;
  padding: 0;
  color: var(--au-text);
  font-weight: 600;
  font-size: 14px;
  cursor: pointer;
  border-radius: var(--au-r-sm);
}
.user-link:hover { color: var(--au-primary); }
.user-link:focus-visible { outline: 2px solid var(--au-border-focus); outline-offset: 2px; }
.user-sub { font-size: 12px; color: var(--au-text-3); }

.pager { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }

/* ===== 详情抽屉 ===== */
.detail-body { min-height: 260px; }

.detail-hero { display: flex; gap: 14px; align-items: center; margin-bottom: 16px; }

.hero-avatar {
  width: 52px;
  height: 52px;
  border-radius: var(--au-r-md);
  display: flex;
  align-items: center;
  justify-content: center;
  font-family: var(--au-font-serif);
  font-size: 22px;
  font-weight: 700;
  color: var(--au-primary);
  background: var(--au-primary-soft);
  border: 1px solid var(--au-primary-border);
  flex-shrink: 0;
}

.hero-main { min-width: 0; }
.hero-name { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 18px; font-weight: 600; color: var(--au-text); }
.hero-sub { font-size: 12px; color: var(--au-text-3); margin-top: 3px; word-break: break-all; }

.detail-stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 10px;
  margin-bottom: 14px;
}
/* 会员格的值是套餐名（文字）：字号收一档，避免长名字换三行 */
.text-tile :deep(.au-stat__value) { font-size: 1.05rem; }

.detail-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 8px; }
.detail-actions .el-button { margin-left: 0; }
.detail-tabs { margin-top: 6px; }

.mini-summary { font-size: 12.5px; color: var(--au-text-2); margin-bottom: 10px; }
.ok { color: var(--au-success); }
.bad { color: var(--au-danger); }

.line-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 0;
  border-bottom: 1px solid var(--au-border);
  font-size: 13px;
  color: var(--au-text);
}
.line-row:last-child { border-bottom: none; }
.line-tag { flex-shrink: 0; }
.line-desc { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.line-amount { font-weight: 600; font-variant-numeric: tabular-nums; }
.line-date { font-size: 11.5px; color: var(--au-text-3); flex-shrink: 0; font-variant-numeric: tabular-nums; }
.mono { font-family: var(--font-mono); }

.dialog-user { font-weight: 600; color: var(--au-text); }
.dialog-hint { font-size: 12px; color: var(--au-text-3); line-height: 1.6; }
.dialog-hint.warn { color: var(--au-warning); }
.welfare-expired { color: var(--au-danger); }
.range-row { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; width: 100%; }
.range-num { width: 120px; }
.range-sep { color: var(--au-text-3); }

/* ===== 授权资源卡片（Phase 4）===== */
.tab-label { display: inline-flex; align-items: center; gap: 4px; }
.grant-skeleton { display: flex; flex-direction: column; gap: 10px; }
.sk-row { height: 56px; border-radius: var(--au-r-md); }
.sk-card { height: 160px; border-radius: var(--au-r-md); }

.grant-summary {
  display: grid;
  /* 104px 下限：620px 抽屉里一行放得下 5 项，手机上一行 3 项自动换行 */
  grid-template-columns: repeat(auto-fit, minmax(104px, 1fr));
  gap: 8px;
  margin-bottom: 12px;
}

.gs-item {
  background: var(--au-surface-2);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-sm);
  padding: 8px 10px;
}
.gs-item.is-warn { border-color: var(--au-warning-border); }
.gs-item.is-warn .gs-value { color: var(--au-warning); }

.gs-label { display: block; font-size: 11.5px; color: var(--au-text-3); }
.gs-value { display: block; margin-top: 2px; font-size: 14px; font-weight: 600; color: var(--au-text); }
.gs-value em { font-style: normal; margin-left: 4px; font-size: 11.5px; font-weight: 400; color: var(--au-text-2); }

.grant-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 10px;
}

.grant-card {
  display: flex;
  flex-direction: column;
  gap: 7px;
  padding: 12px;
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-md);
  /* 抽屉底色是表面色，卡片用次级画布拉开层次（发丝线分层，不靠阴影） */
  background: var(--au-bg-soft);
}

/* 状态色只落在标题行的点上，卡片本体不染色 */
.grant-head { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.grant-name { font-size: 14px; font-weight: 600; color: var(--au-text); }

.grant-dot {
  width: 7px;
  height: 7px;
  border-radius: var(--au-r-full);
  flex-shrink: 0;
  background: var(--au-text-4);
}
.grant-dot.ok { background: var(--au-success); }
.grant-dot.warn { background: var(--au-warning); }

.grant-reason {
  margin: 0;
  font-size: 12.5px;
  color: var(--au-text-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.grant-caps { display: flex; flex-wrap: wrap; gap: 5px; }
.cap {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 1px 7px;
  border-radius: var(--au-r-full);
  border: 1px solid transparent;
  font-size: 11px;
  font-weight: 500;
}
.cap.on { background: var(--au-success-soft); color: var(--au-success); border-color: var(--au-success-border); }
.cap.off { background: var(--au-violet-soft); color: var(--au-text-3); border-color: var(--au-border); }

.grant-metrics {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
  margin-top: 2px;
}
.grant-metric { display: flex; flex-direction: column; gap: 1px; }
.grant-metric b {
  font-size: 16px;
  font-weight: 600;
  color: var(--au-text);
  font-variant-numeric: tabular-nums;
}
.grant-metric b em { font-style: normal; font-size: 11.5px; font-weight: 400; color: var(--au-text-3); }
.grant-metric span { font-size: 11px; color: var(--au-text-3); }

.grant-note {
  display: flex;
  align-items: flex-start;
  gap: 5px;
  margin: 0;
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--au-text-2);
}
.grant-note svg { margin-top: 2px; flex-shrink: 0; }
.grant-note.muted { color: var(--au-text-3); }

.grant-scope {
  margin: 10px 0 0;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--au-text-3);
}

/* flush 卡片里的手机卡片列表：DataTable 本身不留边距，这里补回左右内距 */
.users-page :deep(.dt-cards) { padding: 0 12px 12px; }

/* 公益弹窗里的行内提示（v2.55 公益用户页并入） */
.au-hint {
  margin-left: 8px;
  color: var(--au-text-2);
}

@media (max-width: 768px) {
  .users-toolbar { padding: 10px 16px; }
  .users-filters { flex: 1 1 100%; }
  .f-search { flex: 1 1 100%; width: auto; }
  .f-select { flex: 1 1 calc(50% - 4px); width: auto; min-width: 0; }
  .pager { justify-content: center; }
  .detail-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .grant-metric b { font-size: 14px; }
  .grant-reason { white-space: normal; }
  .line-row { flex-wrap: wrap; row-gap: 2px; }
  .line-desc { flex: 1 1 60%; }
  .line-date { flex: 1 1 100%; }
}
</style>
