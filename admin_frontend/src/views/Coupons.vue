<script setup lang="ts">
/**
 * 优惠券（v2.10.0）
 *
 * 与兑换码的区别要一眼看得出：兑换码是「不花钱直接拿东西」，优惠券是「付费时打折」，
 * 钱照旧走支付网关，只是单价变了。所以这张表讲的全是「折扣与额度」。
 *
 * 额度是**预订制**的：下单就占（reserved），付款转已用（consumed），关单/退款释放（released）。
 * 面板上必须能看见「占用中」这一档——它可能随时变成已用，也可能超时被自动清理，
 * 只看 `use_count` 会说不清「这张券到底还能不能发」。
 *
 * v2.54（暗房影院）：PageHeader + StatTile（总数 / 可用 / 占用中 / 已核销）+
 * 「功能设置」SectionCard（两行设置，保存在标题行右侧）+ flush 券表（筛选左、查询右）；
 * 加载失败给可重试的错误态。弹窗结构不变，只把写死的颜色 / 圆角换成 --au-* 令牌。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import {
  AlertTriangle, CircleCheck, Clock3, Plus, RefreshCw, Search, Settings2, TicketPercent, Tickets,
} from 'lucide-vue-next'
import { EmptyState, PageHeader, SectionCard, StatTile } from '@/components/ui'
import DataTable from '@/components/DataTable.vue'
import type { DataColumn } from '@/components/DataTable.vue'
import { useRealmStore } from '@/stores/realm'
import {
  createCoupons,
  deleteCoupon,
  fetchCouponSettings,
  fetchCouponUsages,
  fetchCoupons,
  updateCoupon,
  updateCouponSettings,
  type CouponCreatePayload,
  type CouponRow,
  type CouponUsageRow,
} from '@/api/economy'

const realm = useRealmStore()
const realms = computed(() => realm.realms)

function scrollToFirstError() {
  requestAnimationFrame(() => {
    document.querySelector('.el-dialog .el-form-item.is-error')?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  })
}

const loading = ref(false)
const coupons = ref<CouponRow[]>([])
const enabled = ref(true)
const reserveHours = ref(24)
const activeUsage = ref(0)
const settingsSaving = ref(false)
/** 券表加载失败（区别于「还没有优惠券」） */
const loadError = ref(false)

// 筛选
const filters = ref({ kind: '', active: '', search: '' })

const columns: DataColumn[] = [
  { key: 'code', label: '优惠码', width: 150, mobile: 'title' },
  { key: 'discount', label: '优惠', width: 150 },
  { key: 'limits', label: '额度', width: 130 },
  { key: 'scope', label: '适用范围', width: 150 },
  { key: 'valid', label: '有效期', width: 150 },
  { key: 'note', label: '备注', minWidth: 130, mobile: 'hide' },
  { key: 'is_active', label: '状态', width: 90 },
  // 记录 / 编辑 / 停用 / 删除收进「管理」弹窗：行里只留一个入口
  { key: 'actions', label: '操作', width: 100, fixed: 'right', align: 'right' },
]

// 核销记录弹窗里的表格列
const usagesColumns: DataColumn[] = [
  { key: 'code', label: '优惠码', width: 130, mobile: 'title' },
  { key: 'username', label: '用户', width: 120 },
  { key: 'order_id', label: '订单', minWidth: 180 },
  { key: 'amount', label: '金额', width: 170 },
  { key: 'status', label: '状态', width: 120 },
  { key: 'time', label: '时间', width: 140 },
]

/** 请求序号：连续改筛选时只采纳最后一次请求，避免旧响应覆盖新结果 */
let listSeq = 0
/** 最近一次实际查询用的关键字：防抖回调据此跳过重复查询 */
let lastSearch = ''

/** 只拉列表：改筛选 / 搜索时用，不再顺带重拉优惠券设置 */
async function loadList() {
  const seq = ++listSeq
  clearTimeout(searchTimer)
  lastSearch = filters.value.search
  loading.value = true
  loadError.value = false
  try {
    const list = await fetchCoupons({
      kind: filters.value.kind || undefined,
      active: filters.value.active || undefined,
      search: filters.value.search || undefined,
      limit: 300,
    })
    if (seq !== listSeq) return
    coupons.value = list.coupons
    // 设置接口读到时以它为准；没读到才用列表附带的开关
    if (!settingsLoaded) enabled.value = list.enabled
  } catch {
    if (seq !== listSeq) return
    // 错误提示由 HTTP 拦截器统一处理；这里只记下失败，给出重试入口
    loadError.value = true
  } finally {
    if (seq === listSeq) loading.value = false
  }
}

let settingsLoaded = false
async function loadSettings() {
  try {
    const settings = await fetchCouponSettings()
    settingsLoaded = true
    enabled.value = settings.enabled
    reserveHours.value = settings.reserve_hours
    activeUsage.value = settings.active_usage
  } catch {
    // 读不到就沿用列表附带的启用状态
  }
}

/** 全量刷新（首屏 / 刷新按钮 / 增删改之后）：列表与设置并行 */
async function load() {
  await Promise.all([loadList(), loadSettings()])
}

/** 搜索框输入防抖：停手 350ms 自动查询（回车 / 清空仍立即查询） */
let searchTimer: ReturnType<typeof setTimeout> | undefined
watch(
  () => filters.value.search,
  (value) => {
    clearTimeout(searchTimer)
    searchTimer = setTimeout(() => {
      if (value !== lastSearch) loadList()
    }, 350)
  },
)
onBeforeUnmount(() => clearTimeout(searchTimer))

const hasFilter = computed(() => !!(filters.value.kind || filters.value.active || filters.value.search))

function resetFilters() {
  filters.value = { kind: '', active: '', search: '' }
  loadList()
}

// ==================== 文案 ====================
function discountText(row: CouponRow): string {
  return row.discount_type === 'percent'
    ? `${(row.value / 10).toFixed(1).replace(/\.0$/, '')} 折（实付 ${row.value}%）`
    : `立减 ¥${row.value.toFixed(2)}`
}

function scopeText(row: CouponRow): string {
  const kind = row.kind === 'all' ? '全部商品' : row.kind === 'subscription' ? '仅会员' : '仅充值'
  return row.realm_name ? `${kind} · ${row.realm_name}` : kind
}

function validText(row: CouponRow): string {
  if (!row.valid_from && !row.valid_until) return '长期有效'
  const from = row.valid_from ? row.valid_from.slice(0, 10) : '即日'
  const until = row.valid_until ? row.valid_until.slice(0, 10) : '不限'
  return `${from} → ${until}`
}

function usageText(row: CouponRow): string {
  const total = row.max_uses ? `${row.max_uses} 次` : '不限'
  return `${row.use_count} / ${total}`
}

const summary = computed(() => {
  const usable = coupons.value.filter((c) => c.usable).length
  const reserved = coupons.value.reduce((acc, c) => acc + (c.stats?.reserved || 0), 0)
  const consumed = coupons.value.reduce((acc, c) => acc + (c.stats?.consumed || 0), 0)
  return { total: coupons.value.length, usable, reserved, consumed }
})

// ==================== 新建 ====================
const createVisible = ref(false)
const creating = ref(false)
const createResult = ref<string[]>([])
const resultVisible = ref(false)
const createFormRef = ref<FormInstance>()
const createForm = ref<CouponCreatePayload & { validMode: 'days' | 'until'; valid_until_text: string }>({
  count: 1,
  code: '',
  kind: 'all',
  discount_type: 'percent',
  value: 90,
  min_amount: 0,
  max_discount: 0,
  realm_id: null,
  max_uses: 0,
  per_user_limit: 1,
  validMode: 'days',
  valid_days: 30,
  valid_until_text: '',
  note: '',
})

const createRules: FormRules = {
  kind: [
    { required: true, message: '请选择适用范围', trigger: 'change' },
  ],
  realm_id: [
    { type: 'number', message: '请选择有效的限定服', trigger: 'change' },
  ],
  discount_type: [
    { required: true, message: '请选择折扣方式', trigger: 'change' },
  ],
  value: [
    { required: true, message: '请输入优惠数值', trigger: 'blur' },
    {
      validator: (_rule, value, callback) => {
        const percent = createForm.value.discount_type === 'percent'
        const min = percent ? 1 : 0.01
        const max = percent ? 100 : 100000
        if (typeof value !== 'number' || !Number.isFinite(value) || value < min || value > max) {
          callback(new Error(percent ? '实付百分比应在 1 到 100 之间' : '减免金额应在 0.01 到 100000 元之间'))
          return
        }
        callback()
      },
      trigger: 'blur',
    },
  ],
  min_amount: [
    { required: true, message: '请输入门槛金额', trigger: 'blur' },
    { type: 'number', min: 0, message: '门槛金额不能小于 0', trigger: 'blur' },
  ],
  max_discount: [
    { required: true, message: '请输入封顶减免金额', trigger: 'blur' },
    { type: 'number', min: 0, message: '封顶减免金额不能小于 0', trigger: 'blur' },
  ],
  max_uses: [
    { required: true, message: '请输入总次数上限', trigger: 'blur' },
    { type: 'number', min: 0, message: '总次数上限不能小于 0', trigger: 'blur' },
  ],
  per_user_limit: [
    { required: true, message: '请输入每人限用次数', trigger: 'blur' },
    { type: 'number', min: 0, message: '每人限用次数不能小于 0', trigger: 'blur' },
  ],
  code: [
    { max: 32, message: '优惠码不能超过 32 个字符', trigger: 'blur' },
  ],
  count: [
    {
      validator: (_rule, value, callback) => {
        if (createForm.value.code) {
          callback()
          return
        }
        if (value === undefined || value === null) {
          callback(new Error('请输入生成数量'))
          return
        }
        if (typeof value !== 'number' || !Number.isFinite(value) || value < 1 || value > 200) {
          callback(new Error('生成数量应在 1 到 200 之间'))
          return
        }
        callback()
      },
      trigger: 'blur',
    },
  ],
  validMode: [
    { required: true, message: '请选择有效期方式', trigger: 'change' },
  ],
  valid_days: [
    { required: true, message: '请输入有效天数', trigger: 'blur' },
    { type: 'number', min: 0, max: 3650, message: '有效天数应在 0 到 3650 之间', trigger: 'blur' },
  ],
  valid_until_text: [
    { required: true, whitespace: true, message: '请输入截止时间', trigger: 'blur' },
  ],
  note: [
    { max: 60, message: '备注不能超过 60 个字符', trigger: 'blur' },
  ],
}

function resetCreateForm() {
  createForm.value = {
    count: 1, code: '', kind: 'all', discount_type: 'percent', value: 90,
    min_amount: 0, max_discount: 0, realm_id: null, max_uses: 0, per_user_limit: 1,
    validMode: 'days', valid_days: 30, valid_until_text: '', note: '',
  }
}

async function handleCreate() {
  try {
    await createFormRef.value?.validate()
  } catch {
    scrollToFirstError()
    return
  }

  creating.value = true
  try {
    const f = createForm.value
    const payload: CouponCreatePayload = {
      count: f.code ? 1 : Number(f.count || 1),
      code: f.code ? f.code.trim().toUpperCase() : undefined,
      kind: f.kind,
      discount_type: f.discount_type,
      value: Number(f.value),
      min_amount: Number(f.min_amount || 0),
      max_discount: Number(f.max_discount || 0),
      realm_id: f.realm_id || null,
      max_uses: Number(f.max_uses || 0),
      per_user_limit: Number(f.per_user_limit || 0),
      note: f.note || undefined,
    }
    if (f.validMode === 'days') {
      if (Number(f.valid_days || 0) > 0) payload.valid_days = Number(f.valid_days)
    } else if (f.valid_until_text) {
      payload.valid_until = f.valid_until_text
    }

    const res = await createCoupons(payload)
    createResult.value = res.coupons.map((c) => c.code)
    resultVisible.value = true
    createVisible.value = false
    ElMessage.success(`已生成 ${res.count} 张优惠券`)
    resetCreateForm()
    load()
  } catch (e: unknown) {
    ElMessage.error((e as Error)?.message || '创建失败')
  } finally {
    creating.value = false
  }
}

function copyCodes() {
  navigator.clipboard.writeText(createResult.value.join('\n'))
  ElMessage.success('已复制全部')
}

// ==================== 编辑 ====================
const editVisible = ref(false)
const editing = ref(false)
const editTarget = ref<CouponRow | null>(null)
const editFormRef = ref<FormInstance>()
const editForm = ref({
  kind: 'all' as CouponRow['kind'],
  discount_type: 'percent' as CouponRow['discount_type'],
  value: 90,
  min_amount: 0,
  max_discount: 0,
  realm_id: null as number | null,
  max_uses: 0,
  per_user_limit: 1,
  valid_until_text: '',
  is_active: true,
  note: '',
})

const editRules: FormRules = {
  kind: [
    { required: true, message: '请选择适用范围', trigger: 'change' },
  ],
  realm_id: [
    { type: 'number', message: '请选择有效的限定服', trigger: 'change' },
  ],
  discount_type: [
    { required: true, message: '请选择折扣方式', trigger: 'change' },
  ],
  value: [
    { required: true, message: '请输入优惠数值', trigger: 'blur' },
    {
      validator: (_rule, value, callback) => {
        const percent = editForm.value.discount_type === 'percent'
        const min = percent ? 1 : 0.01
        const max = percent ? 100 : 100000
        if (typeof value !== 'number' || !Number.isFinite(value) || value < min || value > max) {
          callback(new Error(percent ? '实付百分比应在 1 到 100 之间' : '减免金额应在 0.01 到 100000 元之间'))
          return
        }
        callback()
      },
      trigger: 'blur',
    },
  ],
  min_amount: [
    { required: true, message: '请输入门槛金额', trigger: 'blur' },
    { type: 'number', min: 0, message: '门槛金额不能小于 0', trigger: 'blur' },
  ],
  max_discount: [
    { required: true, message: '请输入封顶减免金额', trigger: 'blur' },
    { type: 'number', min: 0, message: '封顶减免金额不能小于 0', trigger: 'blur' },
  ],
  max_uses: [
    { required: true, message: '请输入总次数上限', trigger: 'blur' },
    { type: 'number', min: 0, message: '总次数上限不能小于 0', trigger: 'blur' },
  ],
  per_user_limit: [
    { required: true, message: '请输入每人限用次数', trigger: 'blur' },
    { type: 'number', min: 0, message: '每人限用次数不能小于 0', trigger: 'blur' },
  ],
  valid_until_text: [
    { type: 'string', message: '请输入有效的截止时间', trigger: 'blur' },
  ],
  is_active: [
    { required: true, type: 'boolean', message: '请选择状态', trigger: 'change' },
  ],
  note: [
    { max: 60, message: '备注不能超过 60 个字符', trigger: 'blur' },
  ],
}

function openEdit(row: CouponRow) {
  editTarget.value = row
  editForm.value = {
    kind: row.kind,
    discount_type: row.discount_type,
    value: row.value,
    min_amount: row.min_amount,
    max_discount: row.max_discount,
    realm_id: row.realm_id,
    max_uses: row.max_uses,
    per_user_limit: row.per_user_limit,
    valid_until_text: row.valid_until ? row.valid_until.slice(0, 16).replace('T', ' ') : '',
    is_active: row.is_active,
    note: row.note || '',
  }
  editVisible.value = true
}

async function handleUpdate() {
  try {
    await editFormRef.value?.validate()
  } catch {
    scrollToFirstError()
    return
  }

  if (!editTarget.value) return
  editing.value = true
  try {
    const f = editForm.value
    await updateCoupon(editTarget.value.id, {
      kind: f.kind,
      discount_type: f.discount_type,
      value: Number(f.value),
      min_amount: Number(f.min_amount || 0),
      max_discount: Number(f.max_discount || 0),
      realm_id: f.realm_id || null,
      max_uses: Number(f.max_uses || 0),
      per_user_limit: Number(f.per_user_limit || 0),
      valid_until: f.valid_until_text || undefined,
      is_active: f.is_active,
      note: f.note,
    })
    ElMessage.success('已保存（只影响之后的订单，已下单的金额不变）')
    editVisible.value = false
    load()
  } catch (e: unknown) {
    ElMessage.error((e as Error)?.message || '保存失败')
  } finally {
    editing.value = false
  }
}

// ==================== 管理弹窗（v2.29.0） ====================
// 以前行尾摆着「记录 / 编辑 / 停用 / 删除」四个链接式按钮，券的额度与占用情况又挤在一格里；
// 现在一个「管理」入口，弹窗里先把这张券的折扣、额度、占用与有效期讲清楚，再动手。
const manage = ref({ visible: false, row: null as CouponRow | null })

function openManage(row: CouponRow) {
  manage.value = { visible: true, row }
}

/** 动作后刷新列表，并把弹窗里的券换成最新快照（额度 / 状态就地变化） */
async function refreshManage(id: number) {
  await load()
  const fresh = coupons.value.find((c) => c.id === id)
  if (fresh) {
    manage.value.row = fresh
  } else {
    // 已删除（或不再符合当前筛选）：弹窗没有可描述的对象了
    manage.value.visible = false
  }
}

/** 从弹窗进编辑：先关管理弹窗，避免两层叠着 */
function editFromManage(row: CouponRow) {
  manage.value.visible = false
  openEdit(row)
}

/** 从弹窗看核销记录：同样先关掉这一层 */
function usagesFromManage(row: CouponRow) {
  manage.value.visible = false
  openUsages(row)
}

async function toggleCoupon(row: CouponRow) {
  // 停用是危险方向：用户端立刻不能用。加一次确认（启用直接放行，不打扰）。
  if (row.is_active) {
    try {
      await ElMessageBox.confirm(
        `停用后，用户端将不能再使用优惠券 ${row.code}；已下的订单不受影响，随时可以再启用。`,
        '停用优惠券',
        {
          type: 'warning',
          confirmButtonText: '停用这张券',
          cancelButtonText: '取消',
          confirmButtonClass: 'el-button--danger',
        },
      )
    } catch {
      return
    }
  }
  try {
    await updateCoupon(row.id, { is_active: !row.is_active })
    row.is_active = !row.is_active
    ElMessage.success(row.is_active ? '已启用' : '已停用')
    await refreshManage(row.id)
  } catch (e: unknown) {
    ElMessage.error((e as Error)?.message || '操作失败')
  }
}

async function handleDelete(row: CouponRow) {
  try {
    await ElMessageBox.confirm(
      `优惠券 ${row.code} 将被永久删除。只有从没用过的券才能删除——有核销记录的请改用停用，否则对账对不上。`,
      '删除优惠券',
      {
        type: 'warning',
        confirmButtonText: '删除这张优惠券',
        cancelButtonText: '取消',
        confirmButtonClass: 'el-button--danger',
      },
    )
  } catch {
    return
  }
  try {
    const res = await deleteCoupon(row.id)
    ElMessage.success(res.message || '已删除')
    await refreshManage(row.id)
  } catch (e: unknown) {
    ElMessage.error((e as Error)?.message || '删除失败')
  }
}

// ==================== 核销记录 ====================
const usagesVisible = ref(false)
const usagesLoading = ref(false)
const usages = ref<CouponUsageRow[]>([])
const usagesFilter = ref<CouponRow | null>(null)

async function openUsages(row?: CouponRow) {
  usagesFilter.value = row || null
  usagesVisible.value = true
  usagesLoading.value = true
  try {
    const res = await fetchCouponUsages({ limit: 100, coupon_id: row?.id })
    usages.value = res.records
  } finally {
    usagesLoading.value = false
  }
}

const STATUS_META: Record<string, { label: string; type: 'success' | 'warning' | 'info' }> = {
  reserved: { label: '预订（未付款）', type: 'warning' },
  consumed: { label: '已使用', type: 'success' },
  released: { label: '已释放', type: 'info' },
}

const statusMeta = (s: string) => STATUS_META[s] || { label: s, type: 'info' as const }

// ==================== 设置 ====================
async function saveSettings() {
  settingsSaving.value = true
  try {
    const res = await updateCouponSettings({
      enabled: enabled.value,
      reserve_hours: Number(reserveHours.value || 0),
    })
    ElMessage.success(enabled.value ? '优惠券已开启' : '优惠券已关闭（用户端不再显示优惠码入口）')
    reserveHours.value = res.reserve_hours
  } catch (e: unknown) {
    ElMessage.error((e as Error)?.message || '保存失败')
  } finally {
    settingsSaving.value = false
  }
}

function fmtTime(iso?: string | null) {
  if (!iso) return '—'
  return iso.slice(0, 16).replace('T', ' ')
}

onMounted(() => {
  if (!realm.realms.length) realm.load().catch(() => {})
  load()
})
</script>

<template>
  <div class="admin-page coupons-page">
    <PageHeader
      eyebrow="运营中心"
      title="优惠券"
      description="付费时抵扣：钱照旧走支付网关，只是单价变了。额度下单即占用，付款转已用，关单 / 退款释放。"
    >
      <template #actions>
        <el-button :icon="RefreshCw" :loading="loading" @click="load">刷新</el-button>
        <el-button :icon="TicketPercent" @click="openUsages()">核销记录</el-button>
        <el-button type="primary" :icon="Plus" @click="createVisible = true">新建优惠券</el-button>
      </template>
    </PageHeader>

    <el-alert
      class="mb-4"
      type="info"
      :closable="false"
      show-icon
      title="分工说明：优惠券在下单支付时直接抵扣（钱仍走支付网关）；兑换码（discount 型）是用户兑换后生成一笔抵扣额度，存在钱包里慢慢用。"
    />

    <section class="stat-row" aria-label="优惠券概况">
      <StatTile label="优惠券" :value="summary.total" suffix="张" :icon="Tickets" hint="当前筛选范围内" />
      <StatTile label="可用" :value="summary.usable" :icon="CircleCheck" :tone="summary.usable > 0 ? 'ok' : 'plain'" />
      <StatTile
        label="占用中"
        :value="summary.reserved"
        :icon="Clock3"
        :tone="summary.reserved > 0 ? 'info' : 'plain'"
        hint="已下单未付款，超时会自动释放"
      />
      <StatTile label="已核销" :value="summary.consumed" :icon="TicketPercent" />
    </section>

    <!-- 开关与预订清理：额度不能被「点了下单没付款」的订单永远占着 -->
    <SectionCard title="功能设置" :icon="Settings2">
      <template #actions>
        <el-button type="primary" plain size="small" :loading="settingsSaving" @click="saveSettings">保存设置</el-button>
      </template>
      <div class="settings">
        <div class="setting">
          <div class="setting-text">
            <span class="setting-label">优惠券功能</span>
            <span class="setting-hint">关闭后用户端不再显示优惠码入口，已下的单不受影响</span>
          </div>
          <el-switch v-model="enabled" active-text="开启" inactive-text="关闭" />
        </div>
        <div class="setting">
          <div class="setting-text">
            <span class="setting-label">超时未支付自动收尾</span>
            <span class="setting-hint">
              小时（0 = 不自动清理）：到点后自动关单并退回优惠额度，当前占用中 {{ activeUsage }} 笔
            </span>
          </div>
          <el-input-number v-model="reserveHours" :min="0" :max="720" :step="6" size="small" />
        </div>
      </div>
    </SectionCard>

    <SectionCard title="全部优惠券" :icon="Tickets" :meta="coupons.length ? `${coupons.length} 张` : ''" flush>
      <div class="view-toolbar">
        <div class="view-toolbar__filters">
          <el-input
            v-model="filters.search"
            class="f-search"
            placeholder="搜索优惠码"
            clearable
            @keyup.enter="loadList"
            @clear="loadList"
          >
            <template #prefix><Search :size="14" /></template>
          </el-input>
          <el-select v-model="filters.kind" class="f-select" placeholder="适用范围" clearable @change="loadList">
            <el-option label="全部商品" value="all" />
            <el-option label="仅会员" value="subscription" />
            <el-option label="仅充值" value="recharge" />
          </el-select>
          <el-select v-model="filters.active" class="f-select" placeholder="状态" clearable @change="loadList">
            <el-option label="启用" value="true" />
            <el-option label="停用" value="false" />
          </el-select>
        </div>
        <div class="view-toolbar__actions">
          <el-button v-if="hasFilter" text @click="resetFilters">清空筛选</el-button>
          <el-button type="primary" :icon="Search" @click="loadList">查询</el-button>
        </div>
      </div>

      <EmptyState v-if="loadError && !coupons.length" :icon="AlertTriangle" title="优惠券加载失败" description="网络或服务暂时不可用，稍后重试。">
        <template #actions><el-button :loading="loading" @click="load">重试</el-button></template>
      </EmptyState>

      <DataTable v-else class="flush-table" :rows="coupons" :columns="columns" :loading="loading" empty="还没有优惠券" empty-description="点右上角「新建优惠券」做第一张券。">
        <template #cell-code="{ row }">
          <span class="mono code">{{ row.code }}</span>
        </template>

        <template #cell-discount="{ row }">
          <span class="discount">{{ discountText(row) }}</span>
          <div v-if="row.min_amount > 0 || row.max_discount > 0" class="sub">
            <span v-if="row.min_amount > 0">满 ¥{{ row.min_amount.toFixed(2) }} 可用</span>
            <span v-if="row.max_discount > 0">· 最多省 ¥{{ row.max_discount.toFixed(2) }}</span>
          </div>
        </template>

        <template #cell-limits="{ row }">
          <span class="num">{{ usageText(row) }}</span>
          <div class="sub">
            每人 {{ row.per_user_limit ? row.per_user_limit + ' 次' : '不限' }}
            <span v-if="row.stats?.reserved">· 占用中 {{ row.stats.reserved }}</span>
          </div>
        </template>

        <template #cell-scope="{ row }">{{ scopeText(row) }}</template>

        <template #cell-valid="{ row }">
          <span :class="row.valid_until ? 'num' : 'faint'">{{ validText(row) }}</span>
        </template>

        <template #cell-note="{ row }">
          <span v-if="!row.note" class="faint">—</span>
          <span v-else>{{ row.note }}</span>
        </template>

        <template #cell-is_active="{ row }">
          <el-tag :type="row.is_active ? 'success' : 'info'" size="small">
            {{ row.is_active ? '启用' : '停用' }}
          </el-tag>
        </template>

        <template #cell-actions="{ row }">
          <!-- 一个入口：核销记录 / 编辑 / 停用 / 删除 都在弹窗里（原来这行有 4 个按钮） -->
          <el-button size="small" plain @click="openManage(row)">管理</el-button>
        </template>

        <template #empty>
          <EmptyState
            compact
            :icon="TicketPercent"
            :title="hasFilter ? '没有符合条件的优惠券' : '还没有优惠券'"
            :description="hasFilter ? '换个条件，或清空筛选看全部。' : '新建一批券，用户付费时输入优惠码即可抵扣。'"
          >
            <template #actions>
              <el-button v-if="hasFilter" size="small" @click="resetFilters">清空筛选</el-button>
              <el-button v-else size="small" type="primary" :icon="Plus" @click="createVisible = true">新建优惠券</el-button>
            </template>
          </EmptyState>
        </template>
      </DataTable>
    </SectionCard>

    <!-- 新建 -->
    <el-dialog v-model="createVisible" title="新建优惠券" width="min(560px, 92vw)">
      <el-form ref="createFormRef" :model="createForm" :rules="createRules" label-position="top">
        <div class="form-grid">
          <el-form-item label="适用范围" prop="kind">
            <el-select v-model="createForm.kind" style="width: 100%">
              <el-option label="全部商品（充值 + 会员）" value="all" />
              <el-option label="仅会员" value="subscription" />
              <el-option label="仅充值" value="recharge" />
            </el-select>
          </el-form-item>
          <el-form-item label="限定服（可选）" prop="realm_id">
            <el-select v-model="createForm.realm_id" clearable placeholder="不限定" style="width: 100%">
              <el-option v-for="r in realms" :key="r.id" :label="r.name" :value="r.id" />
            </el-select>
          </el-form-item>
          <el-form-item label="折扣方式" prop="discount_type">
            <el-radio-group v-model="createForm.discount_type">
              <el-radio-button value="percent">按比例打折</el-radio-button>
              <el-radio-button value="fixed">固定减免</el-radio-button>
            </el-radio-group>
          </el-form-item>
          <el-form-item :label="createForm.discount_type === 'percent' ? '实付百分比（90 = 九折）' : '减免金额（元）'" prop="value">
            <el-input-number
              v-model="createForm.value"
              :min="createForm.discount_type === 'percent' ? 1 : 0.01"
              :max="createForm.discount_type === 'percent' ? 100 : 100000"
              :precision="createForm.discount_type === 'percent' ? 0 : 2"
              style="width: 100%"
            />
          </el-form-item>
          <el-form-item label="门槛（满多少可用，0 = 不限）" prop="min_amount">
            <el-input-number v-model="createForm.min_amount" :min="0" :precision="2" style="width: 100%" />
          </el-form-item>
          <el-form-item label="封顶减免（0 = 不封顶）" prop="max_discount">
            <el-input-number v-model="createForm.max_discount" :min="0" :precision="2" style="width: 100%" />
          </el-form-item>
          <el-form-item label="总次数上限（0 = 不限）" prop="max_uses">
            <el-input-number v-model="createForm.max_uses" :min="0" style="width: 100%" />
          </el-form-item>
          <el-form-item label="每人限用（0 = 不限）" prop="per_user_limit">
            <el-input-number v-model="createForm.per_user_limit" :min="0" style="width: 100%" />
          </el-form-item>
        </div>

        <el-divider content-position="left">生成方式</el-divider>
        <div class="form-grid">
          <el-form-item label="指定优惠码（留空 = 随机生成）" prop="code">
            <el-input v-model="createForm.code" placeholder="如 SAVE10" maxlength="32" />
          </el-form-item>
          <el-form-item label="数量（随机码时有效，最多 200）" prop="count">
            <el-input-number v-model="createForm.count" :min="1" :max="200" :disabled="!!createForm.code" style="width: 100%" />
          </el-form-item>
          <el-form-item label="有效期" prop="validMode">
            <el-radio-group v-model="createForm.validMode">
              <el-radio-button value="days">按天数</el-radio-button>
              <el-radio-button value="until">指定时间</el-radio-button>
            </el-radio-group>
          </el-form-item>
          <el-form-item v-if="createForm.validMode === 'days'" label="有效天数（0 = 长期）" prop="valid_days">
            <el-input-number v-model="createForm.valid_days" :min="0" :max="3650" style="width: 100%" />
          </el-form-item>
          <el-form-item v-else label="截止时间" prop="valid_until_text">
            <el-input v-model="createForm.valid_until_text" placeholder="2026-10-01 12:00" />
          </el-form-item>
        </div>

        <el-form-item label="备注" prop="note">
          <el-input v-model="createForm.note" placeholder="活动名称等（选填）" maxlength="60" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="handleCreate">生成</el-button>
      </template>
    </el-dialog>

    <!-- 生成结果 -->
    <el-dialog v-model="resultVisible" title="生成结果（请保存）" width="min(420px, 92vw)">
      <el-input :model-value="createResult.join('\n')" type="textarea" :rows="10" readonly class="mono" />
      <template #footer>
        <el-button type="primary" @click="copyCodes">复制全部</el-button>
      </template>
    </el-dialog>

    <!-- 编辑 -->
    <el-dialog v-model="editVisible" :title="`编辑优惠券 ${editTarget?.code || ''}`" width="min(560px, 92vw)">
      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="改动只影响之后的订单"
        description="已下单的订单保留当时的优惠快照，金额与记录都不会跟着变。"
        style="margin-bottom: 12px"
      />
      <el-form ref="editFormRef" :model="editForm" :rules="editRules" label-position="top">
        <div class="form-grid">
          <el-form-item label="适用范围" prop="kind">
            <el-select v-model="editForm.kind" style="width: 100%">
              <el-option label="全部商品" value="all" />
              <el-option label="仅会员" value="subscription" />
              <el-option label="仅充值" value="recharge" />
            </el-select>
          </el-form-item>
          <el-form-item label="限定服" prop="realm_id">
            <el-select v-model="editForm.realm_id" clearable placeholder="不限定" style="width: 100%">
              <el-option v-for="r in realms" :key="r.id" :label="r.name" :value="r.id" />
            </el-select>
          </el-form-item>
          <el-form-item label="折扣方式" prop="discount_type">
            <el-radio-group v-model="editForm.discount_type">
              <el-radio-button value="percent">比例</el-radio-button>
              <el-radio-button value="fixed">固定减免</el-radio-button>
            </el-radio-group>
          </el-form-item>
          <el-form-item :label="editForm.discount_type === 'percent' ? '实付百分比（90 = 九折）' : '减免金额（元）'" prop="value">
            <el-input-number
              v-model="editForm.value"
              :min="editForm.discount_type === 'percent' ? 1 : 0.01"
              :max="editForm.discount_type === 'percent' ? 100 : 100000"
              :precision="editForm.discount_type === 'percent' ? 0 : 2"
              style="width: 100%"
            />
          </el-form-item>
          <el-form-item label="门槛" prop="min_amount">
            <el-input-number v-model="editForm.min_amount" :min="0" :precision="2" style="width: 100%" />
          </el-form-item>
          <el-form-item label="封顶减免" prop="max_discount">
            <el-input-number v-model="editForm.max_discount" :min="0" :precision="2" style="width: 100%" />
          </el-form-item>
          <el-form-item label="总次数上限（不能低于已占用）" prop="max_uses">
            <el-input-number v-model="editForm.max_uses" :min="0" style="width: 100%" />
          </el-form-item>
          <el-form-item label="每人限用" prop="per_user_limit">
            <el-input-number v-model="editForm.per_user_limit" :min="0" style="width: 100%" />
          </el-form-item>
          <el-form-item label="截止时间（留空 = 不限）" prop="valid_until_text">
            <el-input v-model="editForm.valid_until_text" placeholder="2026-10-01 12:00" />
          </el-form-item>
          <el-form-item label="状态" prop="is_active">
            <el-switch v-model="editForm.is_active" active-text="启用" inactive-text="停用" />
          </el-form-item>
        </div>
        <el-form-item label="备注" prop="note">
          <el-input v-model="editForm.note" maxlength="60" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="editing" @click="handleUpdate">保存</el-button>
      </template>
    </el-dialog>

    <!-- 核销记录 -->
    <el-dialog
      v-model="usagesVisible"
      :title="usagesFilter ? `核销记录 · ${usagesFilter.code}` : '最近的核销记录'"
      width="min(760px, 92vw)"
    >
      <DataTable
        class="usage-table"
        :rows="usages"
        :columns="usagesColumns"
        :loading="usagesLoading"
        empty="还没有核销记录"
        empty-description="用户下单时用了优惠券，核销记录会出现在这里。"
      >
        <template #cell-code="{ row }"><span class="mono">{{ row.code }}</span></template>

        <template #cell-order_id="{ row }"><span class="mono">{{ row.order_id }}</span></template>

        <template #cell-amount="{ row }">
          ¥{{ row.paid_amount.toFixed(2) }}
          <span class="muted">（原价 ¥{{ row.list_price.toFixed(2) }}，省 ¥{{ row.discount_amount.toFixed(2) }}）</span>
        </template>

        <template #cell-status="{ row }">
          <el-tag :type="statusMeta(row.status).type" size="small">{{ statusMeta(row.status).label }}</el-tag>
        </template>

        <template #cell-time="{ row }">{{ fmtTime(row.created_at) }}</template>

        <template #empty>
          <EmptyState compact :icon="TicketPercent" title="还没有核销记录" description="用户下单使用优惠码后会出现在这里。" />
        </template>
      </DataTable>
    </el-dialog>

    <!--
      优惠券管理（弹窗）：这张券到底怎么抵扣、还能不能用、为什么不能用（额度占满 / 已过期 /
      停用）都在这里；四个动作随状态给出（有核销记录的不能删，只能停用）。
    -->
    <el-dialog v-model="manage.visible" :title="`管理优惠券 ${manage.row?.code || ''}`" width="min(540px, 92vw)">
      <div v-if="manage.row" class="mg-body">
        <div class="mg-head">
          <span class="mg-discount">{{ discountText(manage.row) }}</span>
          <span class="au-badge" :class="manage.row.usable ? 'au-badge-green' : 'au-badge-muted'">
            {{ manage.row.usable ? '可用' : manage.row.is_active ? '暂不可用' : '已停用' }}
          </span>
        </div>

        <div class="kv-list">
          <div class="kv-row"><span class="kv-key">优惠码</span>
            <span class="kv-value mono">{{ manage.row.code }}</span>
          </div>
          <div class="kv-row"><span class="kv-key">适用范围</span><span class="kv-value">{{ scopeText(manage.row) }}</span></div>
          <div class="kv-row"><span class="kv-key">额度</span>
            <span class="kv-value">
              {{ usageText(manage.row) }}（每人 {{ manage.row.per_user_limit ? manage.row.per_user_limit + ' 次' : '不限' }}）
            </span>
          </div>
          <div class="kv-row"><span class="kv-key">占用 / 已用</span>
            <span class="kv-value">
              占用中 {{ manage.row.stats?.reserved || 0 }} · 已用 {{ manage.row.stats?.consumed || 0 }}
            </span>
          </div>
          <div class="kv-row"><span class="kv-key">门槛 / 封顶</span>
            <span class="kv-value">
              {{ manage.row.min_amount > 0 ? `满 ¥${manage.row.min_amount.toFixed(2)}` : '无门槛' }} ·
              {{ manage.row.max_discount > 0 ? `最多省 ¥${manage.row.max_discount.toFixed(2)}` : '不封顶' }}
            </span>
          </div>
          <div class="kv-row"><span class="kv-key">有效期</span><span class="kv-value">{{ validText(manage.row) }}</span></div>
          <div class="kv-row"><span class="kv-key">备注</span><span class="kv-value">{{ manage.row.note || '—' }}</span></div>
        </div>

        <p class="mg-hint">
          停用立即生效（用户端不再能使用，已下的单不受影响）；删除只能删从没用过的券，
          有核销记录的请改用停用（否则对账对不上）。
        </p>
      </div>

      <template #footer>
        <div class="mg-footer">
          <el-button v-if="manage.row" type="danger" plain @click="handleDelete(manage.row)">删除这张券</el-button>
          <div class="mg-footer-right">
            <el-button @click="manage.visible = false">关闭</el-button>
            <template v-if="manage.row">
              <el-button @click="usagesFromManage(manage.row)">核销记录</el-button>
              <el-button
                :type="manage.row.is_active ? 'warning' : 'success'"
                plain
                @click="toggleCoupon(manage.row)"
              >
                {{ manage.row.is_active ? '停用' : '启用' }}
              </el-button>
              <el-button type="primary" @click="editFromManage(manage.row)">编辑</el-button>
            </template>
          </div>
        </div>
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

/* ---------- 功能设置：一行一项，说明在左、控件在右 ---------- */
.settings { display: flex; flex-direction: column; }
.setting {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px 16px;
  flex-wrap: wrap;
  padding: 10px 0;
}
.setting + .setting { border-top: 1px solid var(--au-border); }
.setting:first-child { padding-top: 0; }
.setting:last-child { padding-bottom: 0; }
.setting-text { display: flex; flex-direction: column; gap: 2px; min-width: 0; flex: 1 1 260px; }
.setting-label { font-size: 13px; font-weight: 600; color: var(--au-text); }
.setting-hint { font-size: 12px; line-height: 1.5; color: var(--au-text-3); }

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
.f-select { width: 130px; }

.flush-table :deep(.dt-cards) { padding: 12px 12px 8px; }

/* ---------- 单元格 ---------- */
.code { letter-spacing: 0.06em; font-weight: 600; color: var(--au-primary); }
.discount { font-weight: 600; color: var(--au-text); }
.num { font-variant-numeric: tabular-nums; }
.sub { margin-top: 2px; font-size: 12px; color: var(--au-text-3); }
.faint { color: var(--au-text-4); }
.usage-table :deep(.muted) { font-size: 12px; color: var(--au-text-3); }

/* ---------- 弹窗 ---------- */
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0 16px; }

/* 管理弹窗：详情用全局 .kv-list，只补折扣行与说明 */
.mg-body { display: flex; flex-direction: column; gap: 12px; }
.mg-body .kv-row .kv-value { text-align: left; }
.mg-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.mg-discount { font-size: 16px; font-weight: 700; color: var(--au-text); }
.mg-hint {
  margin: 0;
  padding: 10px 12px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--au-text-3);
  background: var(--au-surface-2);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-md);
}
.mg-footer { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
.mg-footer-right { display: flex; gap: 8px; flex-wrap: wrap; }

@media (max-width: 768px) {
  .view-toolbar { padding: 2px 16px 12px; }
  .view-toolbar__filters > .f-search { flex: 1 1 100%; width: auto; }
  .view-toolbar__filters > .f-select { flex: 1 1 120px; width: auto; }
  .view-toolbar__actions { width: 100%; justify-content: flex-end; }
  .form-grid { grid-template-columns: 1fr; }
}
</style>