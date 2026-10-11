<script setup lang="ts">
/**
 * 钱包 — 余额 / 卡码·兑换码核销 / 订单 / 积分流水
 *
 * v2.43.0：购买功能（充值/订阅/优惠券）已拆到 StoreView（/store）。
 * 原钱包页逻辑： / 积分充值（支付下单）/ 卡码·兑换码·优惠券核销 / 订单记录 / 积分流水
 * 布局：余额卡左右分区（左余额+签到态，右唯一的核销面板）；套餐卡横向结构化行
 *
 * v2.10.1：优惠券并进顶部那一个核销面板。此前「卡码 · 兑换码」在余额卡里、
 * 「优惠码」是分页上另起的一条输入框，同一页两个「输入码 → 应用」的面板，
 * 既割裂又让用户猜手里那张码该填哪边；现在只有一个入口，由后端预检识别来源。
 */
import { ref, computed, onMounted, onActivated, onBeforeUnmount, watch } from 'vue'
import { useRoute, useRouter, RouterLink } from 'vue-router'
import { useUserStore } from '@/stores/user'
import {
  Wallet, TicketCheck, Receipt, RefreshCw, Sparkles, Flame,
  ArrowUpRight, ArrowDownLeft, CircleCheck, Clock, CircleAlert, ChevronRight,
  Undo2, Percent, Zap, Coins, Crown,
} from 'lucide-vue-next'
import {
  pointsApi, checkinApi, exchangeApi, paymentApi, membershipApi, vitalityApi,
  type PointsLogEntry, type TransferConfig,
  type OrderRow, type CheckinStatus, type CodePreview, type VitalityStatus,
} from '@/api/economy'
import { useToast } from '@/composables/useToast'

const toast = useToast()
const route = useRoute()
const router = useRouter()
const userStore = useUserStore()

const loading = ref(true)
const balance = ref(0)
const checkin = ref<CheckinStatus | null>(null)
/** 活力值（仅公益服用户有值，非公益服 403 则为 null 不显示） */
const vitality = ref<VitalityStatus | null>(null)
const showRechargeVitality = ref(false)
const rechargeQty = ref(1)
const recharging = ref(false)
const vitalityPointCost = ref(10)
const orders = ref<OrderRow[]>([])
const logs = ref<PointsLogEntry[]>([])
const tab = ref<'orders' | 'log'>('orders')

// ===== 积分转账（C2）=====
const transferConfig = ref<TransferConfig | null>(null)
const transferRecipient = ref('')
const transferAmount = ref<number | null>(null)
const transferring = ref(false)
/** 手续费预览：向上取整 */
const transferFeePreview = computed(() => {
  if (transferAmount.value == null || transferAmount.value <= 0 || !transferConfig.value) return 0
  const pct = transferConfig.value.fee_pct
  return pct > 0 ? Math.ceil(transferAmount.value * pct / 100) : 0
})
async function loadTransferConfig() {
  try { transferConfig.value = await pointsApi.transferConfig() }
  catch { transferConfig.value = null }
}
async function handleTransfer() {
  const to = transferRecipient.value.trim()
  const amt = transferAmount.value
  if (!to) { toast.error('请输入对方用户名'); return }
  if (!amt || amt <= 0 || !Number.isInteger(amt)) { toast.error('请输入正整数金额'); return }
  transferring.value = true
  try {
    const res = await pointsApi.transfer(to, amt)
    toast.success(`已转给 ${res.recipient} ${res.amount} 积分` + (res.fee > 0 ? `（手续费 ${res.fee} 积分）` : ''))
    transferRecipient.value = ''
    transferAmount.value = null
    balance.value = res.balance
  } catch (e: any) {
    toast.error(e?.response?.data?.detail || '转账失败')
  } finally {
    transferring.value = false
  }
}

// ===== 统一核销入口（卡码 / 兑换码 / 邀请码自动识别）=====
// 卡码（会员时长）与兑换码（积分/订阅）原本是两个输入框，用户得自己判断该填哪个。
// 现在只留一个入口：先向后端预检识别来源，再走对应链路（邀请码给出指引）。
const redeemCode = ref('')
const redeemLoading = ref(false)
const codePreview = ref<CodePreview | null>(null)
const codeNotice = ref('')
const couponHint = ref(false)
const redeemInputRef = ref<HTMLInputElement | null>(null)
/** 核销面板下方提示：卡码/兑换码的错误信息 */
const redeemNotice = computed(() => codeNotice.value)

/** 重新输入时清掉上一轮预检结果，避免残留提示误导 */
function resetCodeFeedback() {
  codePreview.value = null
  codeNotice.value = ''
  couponHint.value = false
}

async function handleRedeem() {
  const code = redeemCode.value.trim()
  if (!code) {
    toast.error('请输入卡码或兑换码')
    return
  }
  redeemLoading.value = true
  codePreview.value = null
  codeNotice.value = ''
  try {
    const preview = await membershipApi.preview(code)

    if (preview.kind === 'coupon') {
      // 钱包页不处理优惠券：引导用户去商店使用
      couponHint.value = true
      return
    }

    if (preview.kind === 'exchange') {
      // 兑换码：无预检态，直接核销（积分或订阅时长）
      if (!exchangeEnabled.value) {
        codeNotice.value = '管理员已关闭兑换，如有兑换码请稍后再试'
        return
      }
      const res = await exchangeApi.redeem(code)
      toast.success(res.message || '兑换成功')
      redeemCode.value = ''
      await Promise.all([refreshBalance(), refreshSubscriptions()])
      return
    }

    if (preview.kind === 'code' && preview.valid) {
      // 会员卡码：先展示类型与天数，确认后再核销
      codePreview.value = preview
      return
    }

    // 邀请码 / 无效码 / 未识别：直接把后端给出的指引显示在面板下方
    codeNotice.value = preview.message || '卡码或兑换码不存在'
  } catch (err: any) {
    const detail = err?.response?.data?.detail
    toast.error(typeof detail === 'string' ? detail : '核销失败，请稍后重试')
  } finally {
    redeemLoading.value = false
  }
}


async function refreshSubscriptions() {
  try {
    await userStore.fetchUser()
  } catch {
    // 会员身份刷新失败不影响核销结果
  }
}

async function confirmCodeRedeem() {
  const code = redeemCode.value.trim()
  if (!code) return
  redeemLoading.value = true
  try {
    const res = await membershipApi.redeem(code)
    toast.success(res.message || '卡码核销成功')
    redeemCode.value = ''
    codePreview.value = null
    // Fix 7：卡码可能发放积分（M1 统一码系统），余额也要刷新
    await Promise.all([refreshBalance(), refreshSubscriptions()])
  } catch (err: any) {
    const detail = err?.response?.data?.detail
    toast.error(typeof detail === 'string' ? detail : '核销失败，请稍后重试')
  } finally {
    redeemLoading.value = false
  }
}


const exchangeEnabled = ref(true)

// ===== 数据加载 =====
async function refreshBalance() {
  try {
    const statusFallback: CheckinStatus | null = null
    const [logRes, status] = await Promise.all([
      pointsApi.log({ limit: 1 }),
      checkinApi.status().catch(() => statusFallback),
    ])
    balance.value = logRes.balance
    if (status) checkin.value = status
  } catch { /* 静默 */ }
}

/** 活力值：用积分续活力 */
async function doRechargeVitality() {
  recharging.value = true
  try {
    const r = await vitalityApi.recharge(rechargeQty.value * vitalityPointCost.value)
    if (vitality.value) vitality.value.vitality = r.vitality
    balance.value = r.points_balance
    showRechargeVitality.value = false
  } catch (e: any) {
    alert(e?.response?.data?.detail || '续活失败，请稍后重试')
  } finally {
    recharging.value = false
  }
}

/** 是否已完成过首屏加载：KeepAlive 缓存命中时走静默刷新 */
const hasLoaded = ref(false)

/** 取 settled 结果：成功用返回值；失败时首屏/手动刷新走兜底，静默刷新返回 undefined（调用方跳过赋值，不碰已有数据） */
function settled<T>(r: PromiseSettledResult<T>, fallback: T, silent: boolean): T | undefined {
  if (r.status === 'fulfilled') return r.value
  return silent ? undefined : fallback
}


async function loadAll(silent = false) {
  if (!silent) loading.value = true
  try {
    const emptyOrders = { orders: [] as OrderRow[] }
    const emptyLogs = { total: 0, balance: 0, logs: [] as PointsLogEntry[] }
    const statusFallback: CheckinStatus | null = null
    const exchangeFallback = { enabled: true }
    const [orderR, logR, statusR, exchangeR, vitalityR] = await Promise.allSettled([
      paymentApi.orders({ limit: 20 }),
      pointsApi.log({ limit: 30 }),
      checkinApi.status(),
      exchangeApi.config(),
      // 活力值：非公益服用户 403，settled 兜底为 null 不展示
      vitalityApi.status(),
    ])
    const orderData = settled(orderR, emptyOrders, silent)
    if (orderData !== undefined) orders.value = orderData.orders || []
    const logData = settled(logR, emptyLogs, silent)
    if (logData !== undefined) {
      logs.value = logData.logs || []
      balance.value = logData.balance
    }
    const checkinStatus = settled(statusR, statusFallback, silent)
    if (checkinStatus !== undefined) checkin.value = checkinStatus
    const exchangeCfg = settled(exchangeR, exchangeFallback, silent)
    if (exchangeCfg !== undefined) exchangeEnabled.value = exchangeCfg.enabled !== false
    const vitalityData = vitalityR.status === 'fulfilled' ? vitalityR.value : null
    vitality.value = vitalityData && vitalityData.success ? vitalityData : null
  } finally {
    loading.value = false
    hasLoaded.value = true
  }
}

const TYPE_META: Record<string, { label: string; income: boolean }> = {
  checkin: { label: '签到', income: true },
  invite: { label: '邀请奖励', income: true },
  invitee: { label: '受邀奖励', income: true },
  rebate: { label: '充值返利', income: true },
  exchange: { label: '兑换码', income: true },
  recharge: { label: '充值', income: true },
  admin_grant: { label: '管理员发放', income: true },
  admin_deduct: { label: '管理员扣除', income: false },
  transfer_out: { label: '转账转出', income: false },
  transfer_in: { label: '转账转入', income: true },
  transfer_fee: { label: '转账手续费', income: false },
}

function typeMeta(t: string) {
  return TYPE_META[t] || { label: t, income: true }
}

function fmtTime(iso?: string | null) {
  if (!iso) return '—'
  return iso.slice(0, 16).replace('T', ' ')
}

function orderStatusMeta(s: string) {
  if (s === 'paid') return { label: '已支付', cls: 'au-badge au-badge-green' }
  if (s === 'pending') return { label: '待支付', cls: 'au-badge au-badge-amber' }
  // 退款 / 关单（v2.9.0）：用户端要说清楚这笔钱的状态，否则只会来问客服
  if (s === 'refunded') return { label: '已退款', cls: 'au-badge au-badge-rose' }
  if (s === 'closed') return { label: '已关闭', cls: 'au-badge au-badge-violet' }
  return { label: s, cls: 'au-badge au-badge-rose' }
}


const paidFlag = computed(() => route.query.paid === '1')

// ===== 支付回跳后的到账轮询 =====
// 网关异步回调可能稍晚于浏览器跳转，这里轮询订单状态，到账后自动刷新积分与会员身份
let payPollTimer: number | null = null
const payPolling = ref(false)

function stopPayPoll() {
  if (payPollTimer !== null) {
    window.clearInterval(payPollTimer)
    payPollTimer = null
  }
  payPolling.value = false
}

async function pollPaymentResult() {
  const targetOrder = (route.query.order as string) || ''
  const paidBefore = new Set(
    orders.value.filter((o) => o.status === 'paid').map((o) => o.order_id),
  )
  let attempts = 0
  payPolling.value = true

  payPollTimer = window.setInterval(async () => {
    attempts += 1
    try {
      const res = await paymentApi.orders({ limit: 20 })
      const list = res.orders || []
      orders.value = list

      const arrived = list.find((o) => {
        if (o.status !== 'paid' || paidBefore.has(o.order_id)) return false
        return targetOrder ? o.order_id === targetOrder : true
      })

      if (arrived) {
        await refreshBalance()
        // 会员身份 / 付费墙状态随之刷新（头像、VIP 标识、详情页提示）
        userStore.fetchUser().catch(() => {})
        toast.success(`支付成功，「${arrived.item_name}」已到账`)
        stopPayPoll()
        return
      }
    } catch {
      /* 轮询失败不打断，等下一轮 */
    }

    if (attempts >= 10) {
      stopPayPoll()
      toast.info('暂未收到支付结果，到账后余额与会员会自动更新（可点刷新）', 5000)
    }
  }, 3000)
}

/** 旧地址兼容：/wallet?tab=recharge|plans 跳到商店 */
const lastHandledEntry = ref('')
function handleEntryQuery() {
  const tabQuery = route.query.tab as string | undefined

  if (tabQuery === 'recharge' || tabQuery === 'plans') {
    const query = { ...route.query }
    delete query.tab
    router.replace({ path: '/store', query: { ...query, tab: tabQuery } })
    return
  }

  if (tabQuery === 'orders' || tabQuery === 'log') {
    tab.value = tabQuery
  }

  const order = route.query.order
  if ((paidFlag.value || order) && !payPolling.value) {
    const key = `${order ?? ''}|${route.query.paid ?? ''}`
    if (lastHandledEntry.value !== key) {
      lastHandledEntry.value = key
      toast.info('支付已提交，正在确认到账结果…', 4000)
      pollPaymentResult()
    }
  }
}

onMounted(async () => {
  await loadAll()
  handleEntryQuery()
  loadTransferConfig()
})

onActivated(() => {
  handleEntryQuery()
  if (hasLoaded.value) {
    loadAll(true)
  }
})

watch(() => route.query.tab, (tabQuery) => {
  const t = tabQuery as string | undefined
  if (t === 'recharge' || t === 'plans') {
    const query = { ...route.query }
    delete query.tab
    router.replace({ path: '/store', query: { ...query, tab: t } })
    return
  }
  if (t === 'orders' || t === 'log') {
    tab.value = t
  }
})

onBeforeUnmount(stopPayPoll)

</script>

<template>
  <div class="au-page wallet-view">
    <div class="wallet-ambiance" aria-hidden="true"></div>

    <!-- 骨架屏：首屏加载时占位 -->
    <div v-if="loading && !hasLoaded" class="wallet-skeleton" aria-hidden="true">
      <div class="au-skeleton sk-balance-num"></div>
      <div class="au-skeleton sk-balance-note"></div>
      <div class="au-skeleton sk-redeem"></div>
      <div class="sk-list">
        <div v-for="n in 5" :key="n" class="sk-row">
          <div class="au-skeleton sk-row-icon"></div>
          <div class="sk-row-text">
            <div class="au-skeleton sk-row-title"></div>
            <div class="au-skeleton sk-row-sub"></div>
          </div>
        </div>
      </div>
    </div>

    <!-- 余额主卡：大数字 + 签到态 + 活力值（核销已独立成区） -->
    <template v-else>
    <section class="balance-hero au-anim-up">
      <div class="bh-main">
        <span class="balance-label">
          <Wallet :size="15" />
          积分余额
        </span>
        <div class="balance-num">
          <span class="num">{{ balance }}</span>
          <span class="unit">积分</span>
        </div>
        <!-- 多服运营下这句必须写明：积分是一份通用的，会员才是一个服一个 -->
        <p class="balance-note">积分与余额全站通用（多服共用一份）；会员是一个服一个。</p>

        <!-- 签到态：紧凑胶囊（签到开关关闭时隐藏） -->
        <RouterLink v-if="checkin && checkin.enabled !== false" to="/checkin" class="checkin-pill" :class="{ done: checkin.checked_today }">
          <Flame :size="13" />
          <span>连签 <strong>{{ checkin.streak }}</strong> 天</span>
          <span class="pill-divider" />
          <span>{{ checkin.checked_today ? '今日已签' : '今日未签' }}</span>
          <ChevronRight :size="12" class="pill-arrow" />
        </RouterLink>

        <!-- 活力值：仅公益服用户 -->
        <div v-if="vitality" class="vitality-row">
          <span class="vitality-icon"><Zap :size="15" /></span>
          <div class="vitality-meta">
            <div class="vitality-top">
              <strong>活力值 {{ vitality.vitality }} / {{ vitality.max }}</strong>
              <span v-if="!vitality.can_play" class="vitality-warn">低于观影阈值 {{ vitality.limit_threshold }}，已限制观影</span>
              <span v-else class="vitality-ok">每日 00:00 自动扣 1 点</span>
            </div>
            <div class="member-bar" role="progressbar" :aria-valuenow="vitality.vitality" aria-valuemin="0" :aria-valuemax="vitality.max">
              <i :style="{ width: (vitality.vitality / vitality.max * 100) + '%' }" />
            </div>
          </div>
          <button type="button" class="vitality-recharge-btn" @click="showRechargeVitality = true">用积分续</button>
        </div>

      </div>
    </section>

    <!-- 快捷操作 -->
    <nav class="wallet-actions au-anim-up" aria-label="快捷操作">
      <RouterLink to="/store?tab=recharge" class="wa-item">
        <Coins :size="18" />
        <span>充值</span>
      </RouterLink>
      <RouterLink to="/store?tab=plans" class="wa-item">
        <Crown :size="18" />
        <span>买会员</span>
      </RouterLink>
      <a v-if="transferConfig?.enabled" href="#wallet-transfer" class="wa-item">
        <ArrowUpRight :size="18" />
        <span>转账</span>
      </a>
    </nav>

    <!-- 核销中心（原余额卡右侧，现独立成区） -->
    <section class="redeem-card au-card au-anim-up" aria-label="核销中心">
      <span class="sec-label">核销中心</span>
      <form class="bh-redeem" @submit.prevent="handleRedeem">
  <label class="redeem-label">
    <TicketCheck :size="14" /> 卡码 · 兑换码
  </label>

  <div class="redeem-row">
    <input
      ref="redeemInputRef"
      class="redeem-input"
      v-model="redeemCode"
      placeholder="输入卡码 / 兑换码"
      maxlength="64"
      @input="resetCodeFeedback"
    />
    <button class="au-btn au-btn-primary" type="submit" :disabled="redeemLoading || !redeemCode.trim()">
      <Sparkles :size="14" />
      <span v-if="redeemLoading" class="spinner"></span>
      使用
    </button>
  </div>

  <p v-if="couponHint" class="redeem-hint warn">
    这是优惠券，请到 <RouterLink to="/store">商店</RouterLink> 使用
  </p>

  <div v-if="codePreview" class="redeem-preview">
    <div class="rp-text">
      <CircleCheck :size="14" />
      <strong>{{ codePreview.type_name }}</strong>
      <span>{{ codePreview.days_text }}</span>
      <span>{{ codePreview.realm_name }}</span>
      <span>{{ codePreview.is_named ? '记名' : '不记名' }}</span>
    </div>
    <button class="au-btn au-btn-primary au-btn-sm" type="button" :disabled="redeemLoading" @click="confirmCodeRedeem">
      确认开通
    </button>
  </div>

  <p class="redeem-hint" :class="{ warn: !!codeNotice }">{{ codeNotice || '会员时长 · 积分 · 兑换码，自动识别' }}</p>

  <button class="au-btn au-btn-ghost au-btn-sm refresh" type="button" title="刷新" @click="() => loadAll()">
    <RefreshCw :size="14" :class="{ spinning: loading }" />
  </button>
      </form>
    </section>

    <!-- 积分转账（C2）：开关关闭时隐藏 -->
    <section v-if="transferConfig?.enabled" id="wallet-transfer" class="au-card transfer-card au-anim-up">
      <div class="transfer-head">
        <ArrowUpRight :size="15" />
        <strong>积分转账</strong>
        <span class="transfer-fee-note">手续费 {{ transferConfig.fee_pct }}%</span>
      </div>
      <div class="transfer-row">
        <input v-model="transferRecipient" class="redeem-input" placeholder="对方用户名" maxlength="50" />
        <input v-model.number="transferAmount" type="number" min="1" class="redeem-input" placeholder="转账金额（积分）" />
        <button class="au-btn au-btn-primary" :disabled="transferring" @click="handleTransfer">
          <span v-if="transferring" class="spinner"></span>
          转账
        </button>
      </div>
      <p class="redeem-hint" v-if="transferFeePreview > 0">
        将扣除 <span class="nw">{{ (transferAmount || 0) }} 积分</span> + <span class="nw">手续费 {{ transferFeePreview }} 积分</span>
      </p>
      <p class="redeem-hint" v-else>输入金额后显示手续费</p>
    </section>

    <!-- 选项卡 -->
    <nav class="tabs au-anim-up" style="animation-delay: 100ms">
      <button class="tab" :class="{ active: tab === 'orders' }" @click="tab = 'orders'">
        <Receipt :size="15" /> 订单
      </button>
      <button class="tab" :class="{ active: tab === 'log' }" @click="tab = 'log'">
        <ArrowDownLeft :size="15" /> 流水
      </button>
    </nav>

    <!-- 订单 -->
    <section v-if="tab === 'orders'" class="tab-body au-anim-up">
      <div v-if="!orders.length" class="au-empty">
        <Receipt :size="30" />
        <p>还没有订单记录</p>
      </div>
      <div v-else class="order-list au-card">
        <div v-for="o in orders" :key="o.order_id" class="order-item">
          <span class="order-icon" :class="{ pending: o.status === 'pending', refunded: o.status === 'refunded' }">
            <Clock v-if="o.status === 'pending'" :size="14" />
            <CircleCheck v-else-if="o.status === 'paid'" :size="14" />
            <Undo2 v-else-if="o.status === 'refunded'" :size="14" />
            <CircleAlert v-else :size="14" />
          </span>
          <div class="order-main">
            <span class="order-name">{{ o.item_name }}</span>
            <span class="order-sub">
              <span class="order-id mono">{{ o.order_id }}</span>
              <span class="order-sep">·</span>
              <span>{{ fmtTime(o.created_at) }}</span>
            </span>
            <span v-if="o.status === 'refunded' && o.refund_reason" class="order-sub order-refund">
              退款原因：{{ o.refund_reason }}
            </span>
            <span v-if="(o.discount_amount || 0) > 0" class="order-sub order-coupon">
              <Percent :size="11" />
              优惠码 {{ o.coupon_code || '已用券' }} 已减 ¥{{ (o.discount_amount || 0).toFixed(2) }}
            </span>
          </div>
          <div class="order-side">
            <span class="order-amount">
              <em v-if="(o.discount_amount || 0) > 0" class="price-was">¥{{ (o.list_price || 0).toFixed(2) }}</em>
              ¥{{ o.amount.toFixed(2) }}
            </span>
            <span :class="orderStatusMeta(o.status).cls">{{ orderStatusMeta(o.status).label }}</span>
          </div>
        </div>
      </div>
    </section>


    <!-- 流水 -->
    <section v-if="tab === 'log'" class="tab-body au-anim-up">
      <div v-if="!logs.length" class="au-empty">
        <ArrowDownLeft :size="30" />
        <p>暂无积分流水</p>
      </div>
      <div v-else class="log-list au-card">
        <div v-for="l in logs" :key="l.id" class="log-item">
          <span class="log-icon" :class="typeMeta(l.type).income ? 'in' : 'out'">
            <ArrowDownLeft v-if="typeMeta(l.type).income" :size="14" />
            <ArrowUpRight v-else :size="14" />
          </span>
          <div class="log-body">
            <span class="log-desc">{{ l.description || typeMeta(l.type).label }}</span>
            <span class="log-time">{{ fmtTime(l.created_at) }}</span>
          </div>
          <div class="log-amount" :class="typeMeta(l.type).income ? 'in' : 'out'">
            {{ typeMeta(l.type).income ? '+' : '' }}{{ l.amount }}
          </div>
        </div>
      </div>
    </section>
    </template>

    <!-- 活力值续活弹窗 -->
    <div v-if="showRechargeVitality" class="result-mask" @click.self="showRechargeVitality = false">
      <div class="result-card vitality-dialog">
        <h3 class="result-title">用积分续活力</h3>
        <p class="dialog-note">1 点活力 = {{ vitalityPointCost }} 积分（当前 {{ vitality?.vitality }}/{{ vitality?.max }}）</p>
        <div class="qty-row">
          <button v-for="n in [1,3,7,14]" :key="n" type="button" class="qty-btn" :class="{ active: rechargeQty === n }" @click="rechargeQty = n">+{{ n }}</button>
        </div>
        <p class="dialog-note">将消耗 {{ rechargeQty * vitalityPointCost }} 积分</p>
        <div class="dialog-actions">
          <button type="button" class="au-btn" @click="showRechargeVitality = false">取消</button>
          <button type="button" class="au-btn au-btn-primary" :disabled="recharging" @click="doRechargeVitality">{{ recharging ? '处理中…' : '确认续活' }}</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.wallet-view {
  display: flex;
  flex-direction: column;
  gap: 1.125rem;
  position: relative;
  max-width: 1080px;
  margin: 0 auto;
  width: 100%;
}

.wallet-ambiance {
  position: absolute;
  inset: 0;
  pointer-events: none;
  z-index: 0;
  background: radial-gradient(ellipse 55% 35% at 20% 0%, rgba(232,168,74,0.09), transparent 70%);
}

/* ===== 钱包页骨架屏 ===== */
.wallet-skeleton {
  display: flex;
  flex-direction: column;
  gap: var(--au-space-md, 16px);
  position: relative;
  z-index: 1;
}
.sk-balance-num {
  width: 45%;
  max-width: 220px;
  height: 44px;
}
.sk-balance-note {
  width: 80%;
  height: 14px;
}
.sk-redeem {
  width: 100%;
  height: 52px;
  border-radius: var(--au-r-md, 12px);
}
.sk-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.sk-row {
  display: flex;
  gap: 12px;
  align-items: center;
}
.sk-row-icon {
  width: 40px;
  height: 40px;
  border-radius: 50%;
  flex-shrink: 0;
}
.sk-row-text {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.sk-row-title {
  width: 60%;
  height: 14px;
}
.sk-row-sub {
  width: 40%;
  height: 12px;
}
@media (prefers-reduced-motion: reduce) {
  .wallet-skeleton .au-skeleton {
    animation: none;
  }
}

.wallet-ambiance::after {
  content: "";
  position: absolute;
  inset: 0;
  opacity: 0.04;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='140' height='140' filter='url(%23n)'/%3E%3C/svg%3E");
}

.wallet-view > *:not(.wallet-ambiance) {
  position: relative;
  z-index: 1;
}

/* 通用区块微标签（与商店页统一的 Section 结构语言） */
.wallet-view .sec-label {
  display: block;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--au-text-3);
  margin: 0 0 10px;
}

/* 快捷操作：余额 Hero 下的第一行动层（金融 App 式） */
.wallet-actions {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(0, 1fr));
  gap: 12px;
}

.wa-item {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 14px 8px;
  background: var(--au-surface);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-lg);
  color: var(--au-text);
  font-size: 14px;
  font-weight: 600;
  text-decoration: none;
  cursor: pointer;
  white-space: nowrap;
  transition:
    transform 0.12s cubic-bezier(0.34, 1.56, 0.64, 1),
    border-color var(--au-fast) var(--au-ease);
}

.wa-item svg {
  width: 18px;
  height: 18px;
  color: var(--au-text-2);
  flex-shrink: 0;
}

.wa-item:hover {
  border-color: var(--au-border-strong);
}

.wa-item:active {
  transform: scale(0.97);
}

/* 核销中心：原余额卡右侧，现独立成区 */
.redeem-card {
  padding: 20px 20px 16px;
  display: flex;
  flex-direction: column;
  position: relative;
}

.redeem-card .bh-redeem {
  width: 100%;
}

#wallet-transfer {
  scroll-margin-top: 76px;
}

html[data-theme='light'] .wallet-ambiance {
  display: none;
}

.balance-hero {
  display: flex;
  flex-direction: column;
  gap: 1rem;
  padding: 1.75rem 1.75rem;
  border-radius: var(--au-r-xl);
  background: var(--au-surface);
  border: 1px solid var(--au-border);
  position: relative;
  overflow: hidden;
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.04);
}

.balance-hero::before {
  content: "";
  position: absolute;
  top: 0;
  left: 10%;
  right: 10%;
  height: 1px;
  background: linear-gradient(90deg, transparent, var(--au-primary-border), transparent);
}

.bh-main {
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 0.375rem;
  min-width: 0;
}

.balance-label {
  display: inline-flex;
  align-items: center;
  gap: 0.375rem;
  font-size: 0.8125rem;
  color: var(--au-text-2);
  font-weight: 700;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}

.balance-num {
  display: flex;
  align-items: baseline;
  gap: 0.5rem;
}

.balance-num .num {
  font-family: var(--au-font-serif);
  font-size: 3rem;
  font-weight: 700;
  color: var(--au-text);
  font-variant-numeric: tabular-nums;
  line-height: 1.1;
  letter-spacing: 0.01em;
  white-space: nowrap;
}

.balance-num .unit {
  font-size: 0.875rem;
  color: var(--au-text-3);
  white-space: nowrap;
}

.balance-note {
  margin: 0.5rem 0 0;
  font-size: 0.8125rem;
  color: var(--au-text-3);
  line-height: 1.6;
}

.checkin-pill {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 0.4375rem;
  width: fit-content;
  margin-top: 0.5rem;
  padding: 0.375rem 0.75rem;
  background: var(--au-warning-soft);
  border: 1px solid var(--au-warning-border);
  border-radius: var(--au-r-full);
  color: var(--au-warning);
  font-size: 0.8125rem;
  font-weight: 600;
  text-decoration: none;
  transition: transform 0.12s cubic-bezier(0.34, 1.56, 0.64, 1), box-shadow 0.15s ease-out;
}

.checkin-pill:hover {
  box-shadow: 0 0 12px var(--au-primary-border);
}

.checkin-pill:active {
  transform: scale(0.95);
}

.checkin-pill.done {
  background: var(--au-success-soft);
  border-color: var(--au-success-border);
  color: var(--au-success);
}

.checkin-pill strong {
  font-weight: 700;
}

.checkin-pill .pill-divider {
  width: 1px;
  height: 12px;
  background: currentColor;
  opacity: 0.3;
}

.checkin-pill .pill-arrow {
  width: 12px;
  height: 12px;
}

.vitality-row {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  margin-top: 0.75rem;
  padding: 0.625rem 0.875rem;
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-lg);
  background: var(--au-surface-2);
}

.vitality-icon {
  color: var(--au-warning);
  display: inline-flex;
  flex-shrink: 0;
}

.vitality-meta {
  flex: 1;
  min-width: 0;
}

.vitality-top {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.5rem;
  font-size: 0.8125rem;
  margin-bottom: 0.375rem;
}

.vitality-warn {
  color: var(--au-danger);
  font-size: 0.75rem;
  font-weight: 600;
}

.vitality-ok {
  color: var(--au-text-3);
  font-size: 0.75rem;
}

.member-bar {
  height: 6px;
  border-radius: var(--au-r-full);
  background: var(--au-track);
  overflow: hidden;
}

.member-bar i {
  display: block;
  height: 100%;
  border-radius: var(--au-r-full);
  background: linear-gradient(90deg, var(--au-gold-b), var(--au-gold-a));
  transition: width 0.4s var(--au-ease);
}

.vitality-recharge-btn {
  flex-shrink: 0;
  padding: 0.375rem 0.875rem;
  border-radius: var(--au-r-full);
  border: 1px solid var(--au-primary);
  color: var(--au-primary);
  background: transparent;
  font-size: 0.75rem;
  font-weight: 700;
  white-space: nowrap;
  cursor: pointer;
  transition: transform 0.12s cubic-bezier(0.34, 1.56, 0.64, 1), box-shadow 0.15s ease-out, background-color 0.15s ease-out;
}

.vitality-recharge-btn:hover {
  background: var(--au-primary-soft);
  box-shadow: 0 0 8px var(--au-primary-border);
}

.vitality-recharge-btn:active {
  transform: scale(0.95);
}

.refresh {
  align-self: flex-start;
  margin-top: 0.25rem;
}

.refresh.spinning {
  animation: au-spin 0.9s linear infinite;
}

@keyframes au-spin {
  to {
    transform: rotate(360deg);
  }
}

.wallet-view button {
  transition: transform 0.12s cubic-bezier(0.34, 1.56, 0.64, 1), border-color 0.15s ease-out, box-shadow 0.15s ease-out, background-color 0.15s ease-out;
}

.wallet-view button:active {
  transform: scale(0.97);
}

.bh-redeem {
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: .625rem;
  min-width: 0;
}

.redeem-label {
  display: inline-flex;
  align-items: center;
  gap: .375rem;
  font-size: .8125rem;
  font-weight: 700;
  letter-spacing: .12em;
  text-transform: uppercase;
  color: var(--au-text-3);
}

.redeem-row {
  display: flex;
  gap: .5rem;
}

input.redeem-input {
  flex: 1;
  min-width: 0;
  background: var(--au-input-bg);
  border: 1px solid var(--au-input-border);
  border-radius: var(--au-r-md);
  padding: 0 .75rem;
  height: 44px;
  color: var(--au-text);
  font-size: .875rem;
}

input.redeem-input::placeholder {
  color: var(--au-text-3);
}

input.redeem-input:focus {
  border-color: var(--au-primary);
  outline: none;
  box-shadow: 0 0 12px var(--au-primary-border);
}

.bh-redeem .au-btn-primary:hover {
  box-shadow: 0 0 12px var(--au-primary-border);
}

.redeem-hint {
  font-size: .75rem;
  color: var(--au-text-3);
  margin: 0;
  line-height: 1.5;
}

.redeem-hint.warn {
  color: var(--au-danger);
}

.redeem-hint a {
  color: var(--au-primary);
}

.redeem-preview {
  display: flex;
  align-items: center;
  gap: .5rem;
  flex-wrap: wrap;
  padding: .625rem .75rem;
  border-radius: var(--au-r-md);
  background: var(--au-primary-soft);
  border: 1px solid var(--au-primary-border);
  font-size: .8125rem;
  color: var(--au-text-2);
}

.rp-text {
  display: flex;
  align-items: center;
  gap: .375rem;
  flex-wrap: wrap;
  flex: 1;
}

.redeem-preview svg {
  width: 14px;
  height: 14px;
  color: var(--au-primary);
  flex-shrink: 0;
}

.redeem-preview strong {
  color: var(--au-text);
}

.transfer-card {
  margin-top: 16px;
  padding: 20px;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04);
}

.transfer-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}

.transfer-head svg {
  width: 15px;
  height: 15px;
  color: var(--au-primary);
}

.transfer-head strong {
  font-size: 15px;
  font-weight: 700;
  color: var(--au-text);
}

.transfer-fee-note {
  margin-left: auto;
  font-size: 12px;
  color: var(--au-text-3);
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}

.transfer-row {
  display: flex;
  gap: 8px;
}

.transfer-row input.redeem-input {
  flex: 1;
  min-width: 0;
  /* 与核销中心输入框统一：设计系统规定输入框用 --au-r-md（见 neo-noir-tokens.css --neo-input-radius） */
  border-radius: var(--au-r-md);
}

.transfer-row .au-btn-primary:hover {
  box-shadow: 0 0 12px var(--au-primary-border);
}

.nw {
  white-space: nowrap;
}

.tabs {
  display: flex;
  gap: .375rem;
  background: var(--au-surface);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-lg);
  padding: .3125rem;
  overflow-x: auto;
  scrollbar-width: none;
}

.tabs::-webkit-scrollbar {
  display: none;
}

.tab {
  flex: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: .375rem;
  height: 40px;
  padding: 0 .875rem;
  background: none;
  border: none;
  border-radius: var(--au-r-md);
  color: var(--au-text-3);
  font-size: .8125rem;
  font-weight: 600;
  cursor: pointer;
  white-space: nowrap;
  transition: transform var(--au-fast) var(--au-ease), color var(--au-fast) var(--au-ease), background var(--au-fast) var(--au-ease), box-shadow var(--au-fast) var(--au-ease);
}

.tab:hover {
  color: var(--au-text);
  background: var(--au-surface-2);
}

.tab.active {
  background: var(--au-primary);
  color: var(--au-on-primary);
  box-shadow: 0 0 12px var(--au-primary-border);
}

.tab.active:active {
  transform: scale(.97);
}

.tab-body {
  min-height: 200px;
}

.order-list,
.log-list {
  overflow: hidden;
  box-shadow: inset 0 1px 0 var(--au-border);
}

.order-item {
  display: flex;
  align-items: center;
  gap: .875rem;
  padding: 1rem 1.125rem;
  border-bottom: 1px solid var(--au-border);
}

.order-item:last-child {
  border-bottom: none;
}

.order-item:hover {
  background: var(--au-surface-2);
}

.order-icon {
  width: 34px;
  height: 34px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--au-r-md);
  background: var(--au-success-soft);
  color: var(--au-success);
  flex-shrink: 0;
}

.order-icon.pending {
  background: var(--au-warning-soft);
  color: var(--au-warning);
}

.order-icon.refunded {
  background: var(--au-danger-soft);
  color: var(--au-danger);
}

.order-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: .1875rem;
}

.order-name {
  font-size: .875rem;
  font-weight: 600;
  color: var(--au-text);
  text-overflow: ellipsis;
  white-space: nowrap;
  overflow: hidden;
}

.order-sub {
  display: flex;
  align-items: center;
  gap: .375rem;
  font-size: .8125rem;
  color: var(--au-text-3);
  overflow: hidden;
}

.order-id {
  text-overflow: ellipsis;
  white-space: nowrap;
  overflow: hidden;
  font-variant-numeric: tabular-nums;
}

.order-sep {
  opacity: .5;
}

.order-refund {
  color: var(--au-danger);
}

.order-coupon {
  color: var(--au-primary);
}

.order-coupon svg {
  flex-shrink: 0;
  width: 11px;
  height: 11px;
}

.order-side {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: .3125rem;
  flex-shrink: 0;
}

.order-amount {
  font-family: var(--au-font-serif);
  font-size: 1rem;
  font-weight: 700;
  color: var(--au-text);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.price-was {
  margin-right: .375rem;
  font-style: normal;
  font-size: .8125rem;
  font-weight: 500;
  color: var(--au-text-3);
  text-decoration: line-through;
}

.log-item {
  display: flex;
  align-items: center;
  gap: .875rem;
  padding: .875rem 1.125rem;
  border-bottom: 1px solid var(--au-border);
}

.log-item:last-child {
  border-bottom: none;
}

.log-item:hover {
  background: var(--au-surface-2);
}

.log-icon {
  width: 32px;
  height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--au-r-md);
  flex-shrink: 0;
}

.log-icon.in {
  background: var(--au-success-soft);
  color: var(--au-success);
}

.log-icon.out {
  background: var(--au-danger-soft);
  color: var(--au-danger);
}

.log-body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: .125rem;
}

.log-desc {
  font-size: .8125rem;
  color: var(--au-text);
  text-overflow: ellipsis;
  white-space: nowrap;
  overflow: hidden;
}

.log-time {
  font-size: .8125rem;
  color: var(--au-text-3);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.log-amount {
  font-weight: 700;
  font-size: .9375rem;
  font-variant-numeric: tabular-nums;
  flex-shrink: 0;
  white-space: nowrap;
}

.log-amount.in {
  color: var(--au-success);
}

.log-amount.out {
  color: var(--au-danger);
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
}

.vitality-dialog {
  align-items: stretch;
  text-align: left;
  min-width: min(420px, 90vw);
}

.dialog-note {
  margin: 0;
  font-size: .8125rem;
  color: var(--au-text-2);
  line-height: 1.6;
}

.dialog-actions {
  display: flex;
  gap: .75rem;
  justify-content: flex-end;
  margin-top: 1rem;
}

.qty-row {
  display: flex;
  gap: .5rem;
  margin: .75rem 0;
  flex-wrap: wrap;
}

button.qty-btn {
  padding: .5rem 1rem;
  border-radius: var(--au-r-full);
  border: 1px solid var(--au-border);
  background: var(--au-surface-2);
  color: var(--au-text);
  cursor: pointer;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  transition: transform .12s var(--au-ease), border-color .12s var(--au-ease), box-shadow .12s var(--au-ease), color .12s var(--au-ease);
}

button.qty-btn:hover {
  border-color: var(--au-primary);
}

button.qty-btn.active {
  border-color: var(--au-primary);
  color: var(--au-primary);
  background: var(--au-primary-soft);
  box-shadow: 0 0 8px var(--au-primary-border);
}

button.qty-btn:active {
  transform: scale(.95);
}

@media (max-width: 720px) {
  .balance-hero {
    gap: 1.25rem;
    padding: 1.375rem 1.25rem;
  }

  .refresh {
    position: absolute;
    top: 1.125rem;
    right: 1.125rem;
    margin: 0;
  }

  .balance-num .num {
    font-size: 2.25rem;
  }

  .order-item {
    flex-wrap: wrap;
  }
}

@media (max-width: 768px) {
  .transfer-row {
    flex-direction: column;
  }
}

</style>
