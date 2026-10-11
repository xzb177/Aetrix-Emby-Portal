<script setup lang="ts">
/**
 * 商店 — 充积分 / 买会员 / 花积分 / 核销（卡码·兑换码·优惠券）
 * 布局（极简重做）：顶部状态条 + 充积分/买会员双大卡 + 底部核销折叠入口；
 * 深链 ?tab=plans/?tab=recharge 滚动到对应卡片
 *
 * v2.11.0：从 WalletView 拆分。订单记录、积分流水、余额总览、会员等级
 * 搬去钱包页；本页只留「花钱 / 花积分」的入口。优惠券试算不再跟随分页
 * （本页无分页），改为两类商品同时试算、数据刷新后静默重算。
 */
import { ref, computed, watch, onMounted, onActivated, onBeforeUnmount } from 'vue'
import { useRoute } from 'vue-router'
import { useUserStore } from '@/stores/user'
import {
  Coins, TicketCheck, Sparkles, Zap, Crown, ExternalLink, ChevronRight,
  CircleCheck, CircleAlert, TriangleAlert, X, Percent,
} from 'lucide-vue-next'
import {
  pointsApi, exchangeApi, paymentApi, membershipApi, couponApi, currencyApi, memberApi,
  type RechargePackage, type SubscriptionPlan,
  type OrderRow, type PaymentMethod, type CodePreview, type CouponQuote,
  type PointsLogEntry, type MyMemberInfo,
} from '@/api/economy'
import { subscriptionApi, isExpiringSoon, type MySubscription } from '@/api'
import { useToast } from '@/composables/useToast'
import { tgApi, type TgBindStatus } from '@/api/tg'
import TgBindCard from '@/components/TgBindCard.vue'
import EmptyState from '@/components/ui/EmptyState.vue'

const toast = useToast()
const route = useRoute()
const userStore = useUserStore()

// ===== 状态 =====
const loading = ref(true)
const balance = ref(0)
/** 会员等级（含订阅折扣，失败时为 null 不展示） */
const member = ref<MyMemberInfo | null>(null)
const packages = ref<RechargePackage[]>([])
const plans = ref<SubscriptionPlan[]>([])
const methods = ref<PaymentMethod[]>([])

const payMethod = ref('alipay')
const orderLoading = ref<number | null>(null)

// ===== 优惠券（v2.10.0；v2.10.1 收进统一核销入口）=====
// 优惠额度是按「商品」算的（同一张 9 折券，100 元的包和 30 元的会员省得不一样），
// 所以应用时对商品各试算一次，行内直接显示折后价；下单时后端会再算一遍。
const couponEnabled = ref(true)
const couponApplied = ref('')          // 已生效的码（空 = 没在用券）
const couponLoading = ref(false)
const couponError = ref('')
const couponQuotes = ref<Record<string, CouponQuote>>({})
/** 优惠券统一在底部核销中心输入（买会员卡内只保留已应用状态条），试算状态共用 */

/** 一次试算的结果：用不了的商品带上后端给的说明（满减门槛、适用范围等） */
interface QuoteRow {
  key: string
  quote: CouponQuote | null
  detail?: unknown
}

const quoteOf = (kind: 'recharge' | 'subscription', id: number) => couponQuotes.value[`${kind}:${id}`] || null
/** 折后实付（未用券 = 原价），模板里直接取字符串，避免到处写非空断言 */
const paidPrice = (kind: 'recharge' | 'subscription', id: number, price: number) =>
  (quoteOf(kind, id)?.paid_amount ?? price).toFixed(2)
const wasPrice = (kind: 'recharge' | 'subscription', id: number) => {
  const q = quoteOf(kind, id)
  return q ? q.list_price.toFixed(2) : ''
}
const couponSavings = computed(() => {
  const values = Object.values(couponQuotes.value)
  return values.length ? Math.max(...values.map((q) => q.discount_amount)) : 0
})

// 核销面板下面那一行：出错（券用不了 / 邀请码指引 / 卡码无效）优先，其次才是默认口径
const redeemNotice = computed(() => couponError.value || codeNotice.value)
const redeemHint = computed(() => {
  if (couponApplied.value) return '下单时按折后价支付，关单或退款后优惠次数自动退回'
  return '会员时长 · 积分 · 订阅 · 优惠券，自动识别'
})

function clearCoupon() {
  couponApplied.value = ''
  couponError.value = ''
  couponQuotes.value = {}
}

/** 一类商品的试算清单（券是按商品算的，所以先算用户眼前这一页） */
function couponTargets(kind: 'recharge' | 'subscription') {
  return kind === 'subscription'
    ? plans.value.map((p) => ({ kind: 'subscription' as const, id: p.id }))
    : packages.value.map((p) => ({ kind: 'recharge' as const, id: p.id }))
}

async function quoteCoupon(code: string, kind: 'recharge' | 'subscription'): Promise<QuoteRow[]> {
  return await Promise.all(couponTargets(kind).map(async (t): Promise<QuoteRow> => {
    try {
      return {
        key: `${t.kind}:${t.id}`,
        quote: await couponApi.quote({ code, kind: t.kind, item_id: t.id }),
      }
    } catch (err: any) {
      return { key: `${t.kind}:${t.id}`, quote: null, detail: err?.response?.data?.detail }
    }
  }))
}

/**
 * 试算并应用优惠券
 *
 * silent：不弹 toast（数据刷新自动重算时不打扰用户）
 *
 * 本页没有分页，两类商品同时试算：券可能只适用于充值包或只适用于会员套餐，
 * 任一命中即应用，两类的折后价合并进 couponQuotes。
 */
async function applyCoupon(code: string, { silent = false } = {}) {
  const text = (code || '').trim()
  couponError.value = ''
  if (!text) {
    clearCoupon()
    return
  }
  couponLoading.value = true
  try {
    // 公益服没有会员可卖，就不去试订阅了
    const kinds: Array<'recharge' | 'subscription'> = isFreeRealm.value
      ? ['recharge']
      : ['recharge', 'subscription']

    const allRows = (await Promise.all(kinds.map((k) => quoteCoupon(text, k)))).flat()
    const valid = allRows.filter((r) => r.quote)

    if (!valid.length) {
      const detail = allRows.find((r) => r.detail)?.detail
      couponApplied.value = ''
      couponQuotes.value = {}
      couponError.value = typeof detail === 'string'
        ? detail
        : (allRows.length ? '优惠码不可用' : '暂无可购买的商品')
      if (!silent) toast.error(couponError.value)
      return
    }

    couponQuotes.value = Object.fromEntries(valid.map((r) => [r.key, r.quote as CouponQuote]))
    couponApplied.value = valid[0].quote!.code
    // 有些商品不满足这张券（如满减门槛）：说清楚，不默默只给一部分打折
    const skipped = allRows.length - valid.length
    if (skipped > 0) {
      couponError.value = `已应用，但有 ${skipped} 个商品不满足该券条件（原价购买）`
    }
    if (!silent) toast.success(`已应用「${couponApplied.value}」`)
  } finally {
    couponLoading.value = false
  }
}

// ===== 功能开关（管理端可关；关闭时给出提示，不让用户白提交）=====
const rechargeEnabled = ref(true)
const plansEnabled = ref(true)
const exchangeEnabled = ref(true)

// ===== 公益服（v2.7.0）：这个服免费开放，不需要买会员 =====
// 后端 /payment/plans 会下发当前服的接入方式；公益服一律不展示套餐与购买引导，
// 否则用户会以为“不买就看不了”，而实际上他本来就能看。
const isFreeRealm = ref(false)
const realmNote = ref('')

// ===== 当前会员（订阅区顶部状态行）=====
const subscriptions = ref<MySubscription[]>([])
const currentSub = computed(
  () => subscriptions.value.find((s) => s.status === 'active' && s.days_left > 0) || null,
)
// 临期：与后台到期提醒同口径（默认 7 天）——商店页本来就是续费的地方，
// 剩余天数不多时直接把状态行染成警示色，别再让用户自己数天数
const subExpiringSoon = computed(() => isExpiringSoon(currentSub.value))

// ===== 统一核销入口（卡码 / 兑换码 / 邀请码自动识别）=====
// 卡码（会员时长）与兑换码（积分/订阅）原本是两个输入框，用户得自己判断该填哪个。
// 现在只留一个入口：先向后端预检识别来源，再走对应链路（邀请码给出指引）。
const redeemCode = ref('')
const redeemLoading = ref(false)
const codePreview = ref<CodePreview | null>(null)
const codeNotice = ref('')
const redeemInputRef = ref<HTMLInputElement | null>(null)
/** 核销折叠区开关：默认收起，只留一行入口 */
const redeemOpen = ref(false)

/** 重新输入时清掉上一轮预检结果，避免残留提示误导 */
function resetCodeFeedback() {
  codePreview.value = null
  codeNotice.value = ''
}

// 优惠券即时验券：防抖定时器句柄与预检竞态序号
let couponPreviewTimer: ReturnType<typeof setTimeout> | null = null
let couponPreviewToken = 0

/**
 * 预检优惠券：仅当预览结果为有效优惠券且功能开启时，静默试算并应用
 * 通过递增序号 token 丢弃过期结果，避免竞态
 */
async function previewCoupon(code: string) {
  // 与已应用的券相同：跳过，避免重复请求
  if (code === couponApplied.value) {
    return
  }
  const token = ++couponPreviewToken
  try {
    const preview = await membershipApi.preview(code)
    // 竞态丢弃：输入已变化则忽略本轮结果
    if (token !== couponPreviewToken) {
      return
    }
    // 仅优惠券且功能开启时静默试算；卡码/兑换码/无效码不做处理，等用户点"使用"走原流程
    if (preview.kind === 'coupon' && preview.valid && couponEnabled.value) {
      await applyCoupon(code, { silent: true })
    }
  } catch {
    // 网络异常静默忽略：不弹 toast、不写错误提示
  }
}

/**
 * 核销输入防抖预检：输入长度 >= 4 且不在提交中时，800ms 后自动预检优惠券
 * 输入清空时不触发预检，也不取消已应用的券
 */
watch(redeemCode, (code) => {
  // 输入变化：作废上一轮在途预检，并清理未触发的定时器
  couponPreviewToken++
  if (couponPreviewTimer) {
    clearTimeout(couponPreviewTimer)
    couponPreviewTimer = null
  }
  // 长度不足（含空输入）：不触发预检
  if (!code || code.length < 4) {
    return
  }
  couponPreviewTimer = setTimeout(() => {
    couponPreviewTimer = null
    // 提交中跳过预检
    if (redeemLoading.value) {
      return
    }
    void previewCoupon(code)
  }, 800)
})

// 组件卸载时清理防抖定时器
onBeforeUnmount(() => {
  if (couponPreviewTimer) {
    clearTimeout(couponPreviewTimer)
    couponPreviewTimer = null
  }
})

async function handleRedeem() {
  const code = redeemCode.value.trim()
  if (!code) {
    toast.error('请输入卡码、兑换码或优惠券')
    return
  }
  redeemLoading.value = true
  codePreview.value = null
  codeNotice.value = ''
  try {
    const preview = await membershipApi.preview(code)

    if (preview.kind === 'coupon') {
      // 优惠券不核销，是「按商品试算折扣」：同一入口，只是后续动作不同
      if (!preview.valid) {
        codeNotice.value = preview.message || '该优惠券已停用'
        return
      }
      if (!couponEnabled.value) {
        codeNotice.value = '管理员已关闭优惠券，如有券请稍后再试'
        return
      }
      await applyCoupon(code)
      // 用上了就清空输入：已应用的码就在面板里写着，不必再占着输入框
      if (couponApplied.value) redeemCode.value = ''
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
  subscriptions.value = await subscriptionApi.getMine().catch(() => subscriptions.value)
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
    await refreshSubscriptions()
  } catch (err: any) {
    const detail = err?.response?.data?.detail
    toast.error(typeof detail === 'string' ? detail : '核销失败，请稍后重试')
  } finally {
    redeemLoading.value = false
  }
}

// ===== 下单 =====
async function handleOrder(kind: 'recharge' | 'subscription', itemId?: number) {
  if (!methods.value.length) {
    toast.error('暂无可用支付方式，请联系管理员')
    return
  }
  orderLoading.value = itemId
  try {
    // 优惠码在试算通过的商品上才带：后端会按同一套口径再算一遍并占额度
    const quote = quoteOf(kind, itemId)
    const res = await paymentApi.createOrder({
      kind,
      item_id: itemId,
      payment_method: payMethod.value,
      coupon_code: quote ? quote.code : undefined,
    })
    if (res.pay_url) {
      toast.success('正在跳转支付…')
      window.location.href = res.pay_url
    } else {
      toast.error('未获取到支付链接')
    }
  } catch (err: any) {
    const detail = err?.response?.data?.detail
    toast.error(typeof detail === 'string' ? detail : '下单失败，请稍后重试')
  } finally {
    orderLoading.value = null
  }
}

// ===== 自定义金额充值（P2）=====
const customAmount = ref<number | null>(null)
const rechargeRatio = ref(1.2)
// C4：快捷金额
const quickAmounts = ref<number[]>([])
const canCustomRecharge = computed(() => (customAmount.value ?? 0) >= 1)
const customPointsPreview = computed(() => {
  const amt = customAmount.value ?? 0
  if (amt < 1) return ''
  return ` ${Math.floor(amt * rechargeRatio.value)} `
})
async function handleCustomRecharge() {
  const amt = customAmount.value ?? 0
  if (amt < 1 || amt > 100000) { toast.error('请输入 1~100000 元'); return }
  orderLoading.value = -1
  try {
    const res = await paymentApi.createOrder({ kind: 'recharge', payment_method: payMethod.value, custom_amount: amt })
    if (res.pay_url) { toast.success('正在跳转支付…'); window.location.href = res.pay_url }
    else toast.error('未获取到支付链接')
  } catch (err: any) {
    toast.error(err?.response?.data?.detail || '下单失败，请稍后重试')
  } finally { orderLoading.value = null }
}

// ===== 数据加载 =====
async function refreshBalance() {
  try {
    // 只取余额：流水明细在钱包页展示
    const res = await pointsApi.log({ limit: 1 })
    balance.value = res.balance
  } catch { /* 静默 */ }
}

/** 是否已完成过首屏加载：KeepAlive 缓存命中时走静默刷新 */
const hasLoaded = ref(false)

/** 取 settled 结果：成功用返回值；失败时首屏/手动刷新走兜底，静默刷新返回 undefined（调用方跳过赋值，不碰已有数据） */
function settled<T>(r: PromiseSettledResult<T>, fallback: T, silent: boolean): T | undefined {
  if (r.status === 'fulfilled') return r.value
  return silent ? undefined : fallback
}

async function loadAll(silent = false) {
  // silent = true 时为 KeepAlive 切回 tab 的后台静默刷新：不碰 loading，
  // 不闪骨架屏，数据到了直接更新视图
  if (!silent) loading.value = true
  try {
    const emptyPkgs = { enabled: false, packages: [] as RechargePackage[] }
    // 兜底也要带上接入方式字段：公益服下接口失败时不能退回“付费服”的口径
    const emptyPlans = {
      enabled: false,
      plans: [] as SubscriptionPlan[],
      access_mode: 'paid' as 'paid' | 'free',
      is_free: false,
      access_note: '',
    }
    const emptyMethods: PaymentMethod[] = []
    const emptyLogs = { total: 0, balance: 0, logs: [] as PointsLogEntry[] }
    const emptySubs: MySubscription[] = []
    const couponFallback = { enabled: false }
    const [pkgR, planR, methodR, logR, subsR, couponR, memberR] = await Promise.allSettled([
      paymentApi.packages(),
      paymentApi.plans(),
      paymentApi.methods(),
      pointsApi.log({ limit: 1 }),
      subscriptionApi.getMine(),
      // 接口失败时按「关闭」处理：宁可不展示，也不让用户填完码才报错
      couponApi.config(),
      // 会员等级：失败时走 settled 兜底为 null，不展示折扣
      memberApi.info(),
    ])
    const pkg = settled(pkgR, emptyPkgs, silent)
    if (pkg !== undefined) {
      packages.value = pkg.packages || []
      rechargeEnabled.value = pkg.enabled !== false
    }
    const plan = settled(planR, emptyPlans, silent)
    if (plan !== undefined) {
      plans.value = plan.plans || []
      isFreeRealm.value = plan.is_free === true || plan.access_mode === 'free'
      realmNote.value = plan.access_note || ''
      plansEnabled.value = plan.enabled !== false
    }
    const methodList = settled(methodR, emptyMethods, silent)
    if (methodList !== undefined) methods.value = Array.isArray(methodList) ? methodList : []
    const logData = settled(logR, emptyLogs, silent)
    if (logData !== undefined) balance.value = logData.balance
    const subs = settled(subsR, emptySubs, silent)
    if (subs !== undefined) subscriptions.value = Array.isArray(subs) ? subs : []
    const couponCfg = settled(couponR, couponFallback, silent)
    if (couponCfg !== undefined) couponEnabled.value = couponCfg.enabled === true
    const memberData = settled(memberR, null, silent)
    if (memberData !== undefined) member.value = memberData
    // 商品与价格回来后，已应用的券要按最新价格重算一次
    // （替代原来的 watch(tab)：本页没有分页，只在数据刷新时重算）
    if (couponApplied.value) void applyCoupon(couponApplied.value, { silent: true })
  } finally {
    loading.value = false
    hasLoaded.value = true
  }
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
  // paidBefore 基准：轮询前先拉一次订单，区分「以前就支付过的」和「新到账的」
  let paidBefore = new Set<string>()
  try {
    const base = await paymentApi.orders({ limit: 20 })
    paidBefore = new Set(
      (base.orders || []).filter((o) => o.status === 'paid').map((o) => o.order_id),
    )
  } catch {
    /* 拉取失败则按空集处理，任何已支付订单都会被视为新到账 */
  }
  let attempts = 0
  payPolling.value = true

  payPollTimer = window.setInterval(async () => {
    attempts += 1
    try {
      const res = await paymentApi.orders({ limit: 20 })
      const list: OrderRow[] = res.orders || []

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

/** 进入时的 query 处理：?order= / ?paid=1 触发支付到账轮询。
 * KeepAlive 下 query 变化不再触发重建，所以 onMounted / onActivated 都要走这里；
 * 用 lastHandledEntry 去重，避免同一笔订单反复弹"支付已提交"。 */
const lastHandledEntry = ref('')
function handleEntryQuery() {
  const entryKey = `${String(route.query.order ?? '')}|${String(route.query.paid ?? '')}`
  if ((paidFlag.value || route.query.order) && !payPolling.value && entryKey !== lastHandledEntry.value) {
    lastHandledEntry.value = entryKey
    toast.info('支付已提交，正在确认到账结果…', 4000)
    pollPaymentResult()
  }
}

/** 深链：/store?tab=plans 滚到买会员卡，/store?tab=recharge 滚到充积分卡 */
function scrollToStoreCard() {
  const tab = String(route.query.tab ?? '')
  const id = tab === 'plans' ? 'card-plans' : tab === 'recharge' ? 'card-recharge' : ''
  if (!id) return
  // 等两帧再滚：首屏骨架屏高度与数据回来后不一致，晚一点滚更准
  requestAnimationFrame(() => requestAnimationFrame(() => {
    document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }))
}

onMounted(async () => {
  await loadAll()
  handleEntryQuery()
  scrollToStoreCard()
  // P2/C4：拉取充值比例与快捷金额（自定义金额换算用），失败时保持默认
  try {
    const info = await currencyApi.info()
    if (info?.recharge_ratio) rechargeRatio.value = info.recharge_ratio
    if (Array.isArray(info?.quick_amounts) && info.quick_amounts.length) quickAmounts.value = info.quick_amounts
  } catch { /* 静默 */ }
  tgApi.status().then(s => { tgStatus.value = s }).catch(() => {})
})

// 从别的 tab 切回来（KeepAlive 缓存命中）：先处理 query，再后台静默刷新
onActivated(() => {
  handleEntryQuery()
  scrollToStoreCard()
  if (hasLoaded.value) void loadAll(true)
})

onBeforeUnmount(stopPayPoll)
const tgStatus = ref<TgBindStatus | null>(null)
const showTgBanner = computed(() => !!tgStatus.value && tgStatus.value.required && !tgStatus.value.bound && !tgStatus.value.in_grace)
</script>

<template>
  <div class="store-page">
    <div class="store-ambiance" aria-hidden="true"></div>
    <TgBindCard v-if="showTgBanner" compact class="store-tg" />

    <section class="store-hero au-anim-up" aria-label="账户概览">
      <div class="sh-balance">
        <span class="sh-label">积分余额</span>
        <RouterLink to="/wallet" class="sh-num">
          <span class="num">{{ balance }}</span>
          <span class="unit">积分</span>
          <ChevronRight class="sh-go" />
        </RouterLink>
      </div>
      <span class="sh-divider" aria-hidden="true"></span>
      <div class="sh-member">
        <Crown class="sh-crown" />
        <div class="sh-member-text">
          <strong v-if="isFreeRealm">公益服</strong>
          <strong v-else-if="currentSub">{{ currentSub.plan_name }}会员</strong>
          <strong v-else>还不是会员</strong>
          <small v-if="isFreeRealm">免费开放 · 无需购买</small>
          <small v-else-if="currentSub">剩 <span class="nowrap">{{ currentSub.days_left }} 天</span> · {{ currentSub.end_date.slice(0, 10) }} 到期</small>
          <small v-else>开通解锁全库播放</small>
        </div>
        <a v-if="!isFreeRealm" class="sh-cta" href="#card-plans">去开通</a>
      </div>
    </section>

    <div class="store-cards">
      <section id="card-recharge" class="store-card au-anim-up">
        <header class="store-card-head">
          <Coins class="store-card-icon" />
          <h2>充积分</h2>
          <small>用人民币购买积分</small>
          <span class="rate-badge"><span class="nowrap">1&nbsp;元</span><span class="rate-eq">=</span><span class="nowrap">{{ rechargeRatio }}&nbsp;积分</span></span>
        </header>

        <div v-if="!rechargeEnabled" class="store-empty">
          <CircleAlert />
          <p>充值通道暂未开启，可先通过签到、邀请或兑换获取积分</p>
        </div>

        <div v-else-if="loading && !packages.length" class="store-skeleton">
          <div v-for="i in 3" :key="i" class="store-skel-row">
            <span class="store-skel-points"></span>
            <span class="store-skel-price"></span>
          </div>
        </div>

        <EmptyState
          v-else-if="!packages.length"
          title="暂时没有充值套餐"
          description="可购买的充值套餐会显示在这里"
        />

        <template v-else>
          <div class="pkg-list">
            <button
              v-for="p in packages"
              :key="p.id"
              type="button"
              class="pkg-row"
              :class="{ popular: p.is_popular }"
              :disabled="orderLoading === p.id || !methods.length"
              @click="handleOrder('recharge', p.id)"
            >
              <span class="pkg-info">
                <span class="nowrap"><strong>{{ p.total_points }}</strong> 积分</span>
                <span class="pkg-name">{{ p.name }}</span>
                <span v-if="p.bonus > 0" class="pkg-bonus"><Sparkles /> 含赠送 <span class="nowrap">{{ p.bonus }} 积分</span></span>
              </span>
              <span class="pkg-price">
                <del v-if="wasPrice('recharge', p.id)"><span class="nowrap">¥{{ wasPrice('recharge', p.id) }}</span></del>
                <span class="pkg-now"><span class="nowrap">¥{{ paidPrice('recharge', p.id, p.price) }}</span></span>
                <em v-if="p.is_popular" class="pkg-badge">超值</em>
              </span>
              <span class="pkg-action">
                <span v-if="orderLoading === p.id" class="spinner"></span>
                <template v-else>购买 <ExternalLink /></template>
              </span>
            </button>
          </div>

          <div class="recharge-amount">
            <span class="sec-label">充值金额</span>
            <div v-if="quickAmounts.length" class="store-quick">
              <div class="store-quick-chips">
                <button
                  v-for="amt in quickAmounts"
                  :key="amt"
                  type="button"
                  class="quick-chip"
                  :class="{ active: customAmount === amt }"
                  @click="customAmount = amt"
                ><span class="nowrap">¥{{ amt }}</span></button>
              </div>
            </div>

            <div class="store-custom">
            <div class="store-custom-row">
              <span class="store-custom-currency">¥</span>
              <input
                v-model.number="customAmount"
                type="number"
                min="1"
                max="100000"
                placeholder="输入金额"
                class="store-custom-input"
              />
              <button
                type="button"
                class="store-custom-btn"
                :disabled="!canCustomRecharge || orderLoading === -1 || !methods.length"
                @click="handleCustomRecharge"
              >
                <span v-if="orderLoading === -1" class="spinner"></span>
                <template v-else>充值<span class="nowrap">{{ customPointsPreview }}积分</span></template>
              </button>
            </div>
            <small class="store-custom-note">按当前比例 <span class="nowrap">{{ rechargeRatio }}</span> 兑换（<span class="nowrap">1 元</span> = <span class="nowrap">{{ rechargeRatio }} 积分</span>）</small>
          </div>
          </div>

          <div v-if="methods.length" class="store-pay">
            <span class="sec-label">支付方式</span>
            <div class="store-pay-group">
              <button
                v-for="m in methods"
                :key="m.id"
                type="button"
                class="store-pay-btn"
                :class="{ active: payMethod === m.id }"
                @click="payMethod = m.id"
              >{{ m.name }}</button>
            </div>
          </div>
        </template>
      </section>

      <section id="card-plans" class="store-card au-anim-up">
        <header class="store-card-head">
          <Zap class="store-card-icon" />
          <h2>买会员</h2>
          <small>开通会员，解锁全库播放</small>
        </header>

        <div v-if="isFreeRealm" class="store-free">
          <Sparkles />
          <p>公益服免费开放，无需购买</p>
        </div>

        <template v-else>
          <div v-if="plansEnabled" class="store-sub" :class="{ warn: subExpiringSoon }">
            <Crown />
            <template v-if="currentSub">
              <span>当前会员：<strong>{{ currentSub.plan_name }}</strong> · 剩 <span class="nowrap"><strong>{{ currentSub.days_left }}</strong> 天</span>（{{ currentSub.end_date.slice(0, 10) }} 到期）</span>
              <template v-if="subExpiringSoon">
                <TriangleAlert />
                <span>即将到期，现在续费可无缝接续</span>
              </template>
            </template>
            <span v-else>当前未开通会员，选择套餐即可解锁全库播放</span>
          </div>

          <div v-if="!plansEnabled" class="store-empty">
            <CircleAlert />
            <p>订阅购买暂未开启，可联系管理员换用卡码开通</p>
          </div>
          <div v-else-if="loading && !plans.length" class="store-skeleton" aria-hidden="true">
            <div v-for="i in 3" :key="i" class="store-skel-row">
              <span class="store-skel-points"></span>
              <span class="store-skel-price"></span>
            </div>
          </div>
          <EmptyState
            v-else-if="!plans.length"
            title="暂时没有可购买的套餐"
            description="可购买的会员套餐会显示在这里"
          />

          <template v-else>
            <div class="plan-list">
              <article v-for="p in plans" :key="p.id" class="plan-item" :class="{ popular: p.is_popular }">
                <header class="plan-head">
                  <strong class="plan-name">{{ p.name }}</strong>
                  <em v-if="p.is_popular" class="plan-badge">推荐</em>
                  <span v-if="p.realm_name" class="plan-realm">{{ p.realm_name }}</span>
                </header>
                <div class="plan-price">
                  <del v-if="wasPrice('subscription', p.id)"><span class="nowrap">¥{{ wasPrice('subscription', p.id) }}</span></del>
                  <span class="plan-now"><span class="nowrap">¥{{ paidPrice('subscription', p.id, p.price) }}</span></span> / <span class="nowrap">{{ p.duration_days }} 天</span>
                </div>
                <p v-if="p.description" class="plan-desc">{{ p.description }}</p>
                <p v-if="(member?.discount_pct || 0) > 0" class="plan-member-hint">
                  <Percent :size="12" /> 会员 {{ (100 - (member?.discount_pct || 0)) / 10 }} 折，下单自动抵扣
                </p>
                <ul v-if="p.features && p.features.length" class="plan-features">
                  <li v-for="(f, i) in p.features" :key="i"><CircleCheck /> {{ f }}</li>
                </ul>
                <div class="plan-actions">
                  <button
                    type="button"
                    class="plan-buy"
                    :disabled="orderLoading === p.id || !methods.length"
                    @click="handleOrder('subscription', p.id)"
                  >
                    <span v-if="orderLoading === p.id" class="spinner"></span>
                    <template v-else><span class="nowrap">¥{{ paidPrice('subscription', p.id, p.price) }}</span> 开通</template>
                  </button>
                </div>
              </article>
            </div>

            <div v-if="couponApplied" class="coupon-strip">
              <Percent />
              <span>优惠券 <strong>{{ couponApplied }}</strong> 已应用 · 下单自动抵扣</span>
              <span v-if="couponSavings > 0" class="coupon-savings">最高省 <span class="nowrap">¥{{ couponSavings.toFixed(2) }}</span></span>
              <button type="button" class="coupon-clear" @click="clearCoupon" aria-label="取消优惠券"><X /></button>
            </div>
            <a v-else-if="couponEnabled" class="coupon-goto" href="#store-redeem" @click="redeemOpen = true">
              <TicketCheck />
              <span>有优惠券？去核销中心使用</span>
              <ChevronRight />
            </a>
            <p v-if="couponError" class="coupon-error">{{ couponError }}</p>
          </template>
        </template>
      </section>
    </div>

    <section id="store-redeem" class="store-redeem au-anim-up" aria-label="核销中心">
      <span class="sec-label">核销中心</span>
      <button
        type="button"
        class="redeem-toggle"
        @click="redeemOpen = !redeemOpen"
        :aria-expanded="redeemOpen"
      >
        <TicketCheck />
        <span>卡码 / 兑换码 / 优惠券</span>
        <ChevronRight :class="{ open: redeemOpen }" />
      </button>

      <div class="redeem-collapse" :class="{ open: redeemOpen }">
      <div class="redeem-collapse-inner">
      <div class="redeem-panel">
        <form class="redeem-form" @submit.prevent="handleRedeem">
          <input
            ref="redeemInputRef"
            v-model="redeemCode"
            placeholder="输入卡码 / 兑换码 / 优惠券"
            maxlength="64"
            autocomplete="off"
            class="redeem-input"
            @input="resetCodeFeedback"
          />
          <button
            type="submit"
            class="redeem-submit"
            :disabled="redeemLoading || !redeemCode.trim()"
          >
            <span v-if="redeemLoading" class="spinner"></span>
            <template v-else>使用</template>
          </button>
        </form>

        <div v-if="codePreview" class="redeem-preview">
          <CircleCheck />
          <span>{{ codePreview.type_name }} · <strong><span class="nowrap">{{ codePreview.days_text }}</span></strong></span>
          <span v-if="codePreview.realm_name"> · 开「{{ codePreview.realm_name }}」的会员</span>
          <span v-if="codePreview.is_named"> · 限指定账号</span>
          <button
            type="button"
            class="redeem-confirm"
            :disabled="redeemLoading"
            @click="confirmCodeRedeem"
          >
            <span v-if="redeemLoading" class="spinner"></span>
            <template v-else>确认开通</template>
          </button>
        </div>
        <div v-else-if="couponApplied" class="redeem-coupon">
          <Percent />
          <span>优惠券 <strong>{{ couponApplied }}</strong> 已应用</span>
          <button type="button" class="redeem-clear" @click="clearCoupon"><X /></button>
        </div>

        <p class="redeem-note" :class="{ warn: !!redeemNotice }">{{ redeemNotice || redeemHint }}</p>
      </div>
      </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.store-ambiance {
  position: absolute;
  inset: 0;
  pointer-events: none;
  z-index: 0;
  background:
    radial-gradient(ellipse 60% 40% at 15% 0%, color-mix(in srgb, var(--au-gold-a) 10%, transparent), transparent 70%),
    radial-gradient(ellipse 50% 35% at 85% 8%, color-mix(in srgb, var(--au-gold-a) 7%, transparent), transparent 70%);
}

.store-ambiance::after {
  content: "";
  position: absolute;
  inset: 0;
  opacity: 0.04;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='140' height='140' filter='url(%23n)'/%3E%3C/svg%3E");
}

.store-page {
  position: relative;
  max-width: 1080px;
  margin: 0 auto;
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: 32px;
}

.store-page > *:not(.store-ambiance) {
  position: relative;
  z-index: 1;
}

html[data-theme='light'] .store-ambiance {
  display: none;
}

/* 账户 Hero：余额大数字 + 会员状态（信息架构第一层） */
.store-hero {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 1px minmax(0, 1fr);
  align-items: stretch;
  gap: 20px;
  padding: 20px 24px;
  background: var(--au-surface);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-xl);
  box-shadow: inset 0 1px 0 color-mix(in srgb, var(--au-text) 4%, transparent);
  position: relative;
  overflow: hidden;
}

.store-hero::before {
  content: "";
  position: absolute;
  top: 0;
  left: 10%;
  right: 10%;
  height: 1px;
  background: linear-gradient(90deg, transparent, var(--au-primary-border), transparent);
}

.sh-balance {
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 2px;
  min-width: 0;
}

.sh-label {
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--au-text-3);
}

a.sh-num {
  display: inline-flex;
  align-items: baseline;
  gap: 8px;
  text-decoration: none;
  color: var(--au-text);
  min-width: 0;
  max-width: 100%;
  overflow: hidden;
}

a.sh-num .num {
  font-family: var(--au-font-serif);
  font-size: 40px;
  font-weight: 700;
  line-height: 1.1;
  letter-spacing: 0.01em;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  min-width: 0;
}

a.sh-num .unit {
  font-size: 13px;
  color: var(--au-text-3);
  white-space: nowrap;
}

.sh-go {
  width: 16px;
  height: 16px;
  color: var(--au-text-3);
  align-self: center;
}

.sh-divider {
  background: var(--au-border-strong);
}

.sh-member {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}

.sh-crown {
  width: 22px;
  height: 22px;
  color: var(--au-text-2);
  flex-shrink: 0;
}

.sh-member-text {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  flex: 1;
}

.sh-member-text strong {
  font-size: 16px;
  font-weight: 700;
  color: var(--au-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.sh-member-text small {
  font-size: 12px;
  color: var(--au-text-3);
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}

.sh-cta {
  flex-shrink: 0;
  font-size: 13px;
  font-weight: 700;
  color: var(--au-primary);
  text-decoration: none;
  padding: 8px 16px;
  border: 1px solid var(--au-primary-border);
  border-radius: var(--au-r-full);
  background: var(--au-primary-soft);
  white-space: nowrap;
}

/* 通用区块微标签（land-book 式 Section 结构） */
.store-page .sec-label {
  display: block;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--au-text-3);
  margin: 0 0 10px;
}

/* 深链滚动不被顶栏遮住 */
#card-recharge,
#card-plans,
#store-redeem {
  scroll-margin-top: 76px;
}

.store-cards {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 24px;
  align-items: start;
}

.store-card {
  background: var(--au-surface);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-xl);
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: 24px;
  min-width: 0;
  position: relative;
  box-shadow: inset 0 1px 0 color-mix(in srgb, var(--au-text) 4%, transparent);
}

.store-card-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.store-card-head h2 {
  font-size: 22px;
  font-weight: 700;
  letter-spacing: 0.01em;
  color: var(--au-text);
  white-space: nowrap;
}

.store-card-head small {
  font-size: 13px;
  color: var(--au-text-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}

.store-card-icon {
  width: 18px;
  height: 18px;
  color: var(--au-text-2);
}

.rate-badge {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 12px;
  border-radius: var(--au-r-full);
  background: var(--au-primary-soft);
  border: 1px solid var(--au-primary-border);
  color: var(--au-primary);
  font-size: 12px;
  font-weight: 700;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
  align-self: center;
  transition: box-shadow 0.15s ease-out;
}

.rate-badge:hover {
  box-shadow: 0 0 8px var(--au-primary-border);
}

.rate-eq {
  color: var(--au-primary);
  font-weight: 700;
}

.store-page button {
  transition: transform 0.12s cubic-bezier(0.34, 1.56, 0.64, 1),
    border-color var(--au-fast) var(--au-ease),
    box-shadow 0.15s ease-out,
    background var(--au-fast) var(--au-ease);
}

.store-page button:active {
  transform: scale(0.97);
}

.store-page .nowrap {
  white-space: nowrap;
}

.store-page svg {
  flex-shrink: 0;
  vertical-align: -2px;
}

.pkg-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin: 0;
  padding: 0;
}

button.pkg-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 16px;
  background: var(--au-surface-2);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-lg);
  cursor: pointer;
  text-align: left;
  font: inherit;
  transition:
    transform 0.12s cubic-bezier(0.34, 1.56, 0.64, 1),
    border-color var(--au-fast) var(--au-ease),
    box-shadow 0.15s ease-out;
}

button.pkg-row:hover {
  border-color: var(--au-border-strong);
  transform: translateY(-1px);
  box-shadow: 0 0 12px var(--au-primary-border);
}
button.pkg-row.popular:hover {
  box-shadow:
    inset 2px 0 0 var(--au-gold-a),
    0 0 12px var(--au-primary-border);
}

button.pkg-row:not(.popular):hover {
  box-shadow: 0 0 12px var(--au-primary-border);
}

button.pkg-row:active {
  transform: scale(0.98);
}

button.pkg-row:disabled {
  opacity: 0.55;
  cursor: default;
}

button.pkg-row.popular {
  border: 1px solid var(--au-primary-border);
  box-shadow:
    inset 2px 0 0 var(--au-gold-a),
    0 0 12px var(--au-primary-border);
}



.pkg-info {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
  min-width: 0;
}

.pkg-info strong {
  font-size: 22px;
  font-weight: 700;
  color: var(--au-text);
  font-variant-numeric: tabular-nums;
  font-family: var(--au-font-serif);
}

.pkg-name {
  font-size: 13px;
  color: var(--au-text-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 200px;
}

.pkg-bonus {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--au-gold-b);
  white-space: nowrap;
}

.pkg-bonus svg {
  width: 11px;
  height: 11px;
}

.pkg-info span.nowrap {
  font-size: 13px;
  color: var(--au-text-3);
}

.pkg-price {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-shrink: 0;
}

.pkg-price del {
  font-size: 13px;
  color: var(--au-text-3);
}

.pkg-now {
  font-size: 17px;
  font-weight: 700;
  color: var(--au-text);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.pkg-badge {
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: var(--au-r-full);
  color: var(--au-gold-b);
  border: 1px solid var(--au-gold-edge);
  white-space: nowrap;
}

.pkg-action {
  display: inline-flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--au-primary);
  white-space: nowrap;
  flex-shrink: 0;
  min-width: 52px;
}

.pkg-action svg {
  width: 12px;
  height: 12px;
}

.store-quick {
  display: flex;
  align-items: center;
  gap: 12px;
}

.recharge-amount {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px;
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-lg);
  background: var(--au-surface-2);
}

.recharge-amount .sec-label {
  margin-bottom: 0;
}

.store-quick-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  min-width: 0;
}

button.quick-chip {
  padding: 8px 14px;
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-md);
  background: var(--au-input-bg);
  color: var(--au-text);
  font-size: 14px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  cursor: pointer;
  transition:
    transform 0.12s cubic-bezier(0.34, 1.56, 0.64, 1),
    border-color var(--au-fast) var(--au-ease),
    box-shadow 0.15s ease-out;
}

button.quick-chip:hover {
  border-color: var(--au-primary);
}

button.quick-chip:active {
  transform: scale(0.95);
}

button.quick-chip.active {
  border-color: var(--au-primary);
  color: var(--au-primary);
  background: var(--au-primary-soft);
  box-shadow: 0 0 8px var(--au-primary-border);
}

.store-custom {
  border-top: 1px solid var(--au-border);
  padding-top: 24px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.store-custom-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.store-custom-currency {
  font-size: 15px;
  color: var(--au-text-3);
  flex-shrink: 0;
}

input.store-custom-input {
  flex: 1;
  min-width: 0;
  height: 44px;
  padding: 0 12px;
  background: var(--au-input-bg);
  border: 1px solid var(--au-input-border);
  border-radius: var(--au-r-md);
  color: var(--au-text);
  font-size: 15px;
  font-variant-numeric: tabular-nums;
  transition:
    border-color var(--au-fast) var(--au-ease),
    box-shadow 0.15s ease-out;
}

input.store-custom-input:focus {
  border-color: var(--au-primary);
  outline: none;
  box-shadow: 0 0 12px var(--au-primary-border);
}

button.store-custom-btn {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-height: 44px;
  padding: 0 16px;
  background: var(--au-primary);
  color: var(--au-on-primary);
  border: none;
  border-radius: var(--au-r-md);
  font-size: 14px;
  font-weight: 600;
  white-space: nowrap;
  flex-shrink: 0;
  cursor: pointer;
  transition:
    transform 0.12s cubic-bezier(0.34, 1.56, 0.64, 1),
    background var(--au-fast) var(--au-ease),
    box-shadow 0.15s ease-out;
}

button.store-custom-btn:hover {
  background: var(--au-primary-strong);
  box-shadow: 0 0 12px var(--au-primary-border);
}

button.store-custom-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

button.store-custom-btn:active {
  transform: scale(0.97);
}

small.store-custom-note {
  font-size: 12px;
  color: var(--au-text-3);
}

.store-pay {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 4px;
}


.store-pay-group {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

button.store-pay-btn {
  display: inline-flex;
  align-items: center;
  min-height: 36px;
  padding: 0 16px;
  border-radius: var(--au-r-full);
  border: 1px solid var(--au-border);
  background: transparent;
  color: var(--au-text-2);
  font-size: 13px;
  white-space: nowrap;
  cursor: pointer;
  transition:
    transform 0.12s cubic-bezier(0.34, 1.56, 0.64, 1),
    border-color var(--au-fast) var(--au-ease),
    background var(--au-fast) var(--au-ease),
    box-shadow 0.15s ease-out;
}

button.store-pay-btn.active {
  border-color: var(--au-primary);
  color: var(--au-primary);
  background: var(--au-primary-soft);
  font-weight: 600;
  box-shadow: 0 0 8px var(--au-primary-border);
}

button.store-pay-btn:active {
  transform: scale(0.95);
}

.store-sub {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 12px 16px;
  border-radius: var(--au-r-md);
  background: var(--au-surface-2);
  border: 1px solid var(--au-border);
  font-size: 13px;
  color: var(--au-text-2);
}
.store-sub svg {
  width: 15px;
  height: 15px;
  color: var(--au-gold-a);
  flex-shrink: 0;
}
.store-sub strong {
  color: var(--au-text);
  font-variant-numeric: tabular-nums;
}
.store-sub.warn {
  border-color: var(--au-warning);
  background: var(--au-warning-soft);
}

.store-free {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 16px;
  color: var(--au-text-2);
  font-size: 14px;
}
.store-free svg {
  width: 15px;
  height: 15px;
  color: var(--au-primary);
}
.store-free p {
  margin: 0;
}

/* 套餐陈列：移动端横滑轮播（App Store 式，一屏露 1.2 张暗示可滑），桌面端双列网格 */
.plan-list {
  display: grid;
  grid-auto-flow: column;
  grid-auto-columns: min(78%, 320px);
  gap: 12px;
  margin: -8px -4px -12px;
  padding: 8px 4px 12px;
  overflow-x: auto;
  scroll-snap-type: x mandatory;
  -webkit-overflow-scrolling: touch;
  scrollbar-width: none;
}

.plan-list::-webkit-scrollbar {
  display: none;
}

.plan-list > .plan-item {
  scroll-snap-align: start;
}

@media (min-width: 1024px) {
  .plan-list {
    grid-auto-flow: row;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    overflow: visible;
    margin: 0;
    padding: 0;
  }
}

.plan-item {
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-lg);
  padding: 20px;
  background: var(--au-surface-2);
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-width: 0;
  position: relative;
  transition:
    transform 0.12s cubic-bezier(0.34, 1.56, 0.64, 1),
    border-color var(--au-fast) var(--au-ease),
    box-shadow 0.15s ease-out;
}
.plan-item:hover {
  border-color: var(--au-border-strong);
  transform: translateY(-2px);
}
.plan-item.popular {
  border: 1px solid var(--au-primary-border);
  box-shadow: 0 0 12px var(--au-primary-border);
}
.plan-item.popular:hover {
  box-shadow: 0 0 12px var(--au-primary-border);
}

.plan-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin: 0;
}
.plan-name {
  font-size: 16px;
  font-weight: 650;
  color: var(--au-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}
em.plan-badge {
  font-style: normal;
  font-size: 11px;
  font-weight: 600;
  padding: 3px 10px;
  border-radius: var(--au-r-full);
  background: var(--au-primary-soft);
  color: var(--au-primary);
  white-space: nowrap;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}
.plan-realm {
  font-size: 12px;
  color: var(--au-text-3);
  white-space: nowrap;
}

.plan-price {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 14px;
  color: var(--au-text-2);
}
.plan-price del {
  font-size: 13px;
  color: var(--au-text-3);
}
.plan-now {
  font-family: var(--au-font-serif);
  font-size: 28px;
  font-weight: 700;
  color: var(--au-text);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  letter-spacing: 0.01em;
}
.plan-perday {
  font-size: 12px;
  color: var(--au-text-3);
  white-space: nowrap;
}

p.plan-desc {
  font-size: 13px;
  color: var(--au-text-3);
  margin: 0;
  line-height: 1.6;
}

.plan-member-hint {
  display: flex;
  align-items: center;
  gap: 4px;
  margin: 4px 0 0;
  font-size: 12px;
  font-weight: 600;
  color: var(--au-primary);
  white-space: nowrap;
}
.plan-member-hint svg {
  width: 12px;
  height: 12px;
}

ul.plan-features {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
ul.plan-features li {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  font-size: 13px;
  color: var(--au-text-2);
  line-height: 1.5;
}
ul.plan-features li svg {
  width: 13px;
  height: 13px;
  color: var(--au-primary);
  flex-shrink: 0;
  margin-top: 2px;
}

.plan-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 8px;
}
button.plan-buy {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  min-height: 44px;
  padding: 0 20px;
  background: var(--au-primary);
  color: var(--au-on-primary);
  border: none;
  border-radius: var(--au-r-md);
  font-size: 14px;
  font-weight: 700;
  cursor: pointer;
  white-space: nowrap;
  flex-shrink: 0;
}
button.plan-buy:hover {
  background: var(--au-primary-strong);
  box-shadow: 0 0 12px var(--au-primary-border);
}
button.plan-buy:disabled {
  opacity: 0.5;
}
button.plan-buy:active {
  transform: scale(0.97);
}

.coupon-strip {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 13px;
  color: var(--au-text-2);
  padding: 10px 14px;
  border: 1px dashed var(--au-primary-border);
  border-radius: var(--au-r-md);
  background: var(--au-primary-soft);
}
.coupon-strip svg {
  width: 14px;
  height: 14px;
  color: var(--au-primary);
  flex-shrink: 0;
}
.coupon-strip strong {
  color: var(--au-text);
}
a.coupon-goto {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--au-text-3);
  text-decoration: none;
  padding: 4px 0;
  white-space: nowrap;
}
a.coupon-goto svg {
  width: 14px;
  height: 14px;
}
a.coupon-goto:hover {
  color: var(--au-primary);
}

.coupon-strip strong {
  color: var(--au-text);
}
.coupon-savings {
  color: var(--au-gold-b);
  font-variant-numeric: tabular-nums;
  font-weight: 700;
}
button.coupon-clear {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 36px;
  padding: 0 12px;
  background: transparent;
  border: 1px solid var(--au-border-strong);
  color: var(--au-text-2);
  border-radius: var(--au-r-md);
  font-size: 13px;
  cursor: pointer;
}
button.coupon-clear:hover {
  border-color: var(--au-primary);
  color: var(--au-text);
}
button.coupon-clear svg {
  width: 13px;
  height: 13px;
}
button.coupon-clear:active {
  transform: scale(0.95);
}
p.coupon-error {
  font-size: 13px;
  color: var(--au-danger);
  margin: 0;
}

/* ===== Redeem section ===== */
.store-redeem {
  margin-top: 8px;
}

.store-redeem button.redeem-toggle {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  background: transparent;
  border: none;
  padding: 8px 2px;
  color: var(--au-text-3);
  font: inherit;
  font-size: 13px;
  cursor: pointer;
  text-align: left;
}

.store-redeem button.redeem-toggle:hover {
  color: var(--au-text-2);
}

.store-redeem button.redeem-toggle svg:first-child {
  width: 14px;
  height: 14px;
  flex-shrink: 0;
}

.store-redeem button.redeem-toggle svg:last-child {
  width: 14px;
  height: 14px;
  margin-left: auto;
  transition: transform var(--au-fast) var(--au-ease);
}

.store-redeem button.redeem-toggle.open svg:last-child {
  transform: rotate(90deg);
}

/* ===== Morph expand (60fps) ===== */
.redeem-collapse {
  display: grid;
  grid-template-rows: 0fr;
  transition: grid-template-rows .3s var(--au-ease);
}

.redeem-collapse.open {
  grid-template-rows: 1fr;
}

.redeem-collapse-inner {
  overflow: hidden;
  min-height: 0;
}

.redeem-collapse .redeem-panel {
  opacity: 0;
  transform: translateY(-6px);
  transition: opacity .25s ease .05s, transform .25s ease .05s;
}

.redeem-collapse.open .redeem-panel {
  opacity: 1;
  transform: none;
}

/* ===== Redeem panel + form ===== */
.redeem-panel {
  margin-top: 8px;
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-lg);
  padding: 16px;
  background: var(--au-surface);
  display: flex;
  flex-direction: column;
  gap: 12px;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04);
}

.redeem-form {
  display: flex;
  gap: 8px;
}

input.redeem-input {
  flex: 1;
  min-width: 0;
  background: var(--au-input-bg);
  border: 1px solid var(--au-input-border);
  border-radius: var(--au-r-md);
  padding: 0 12px;
  height: 44px;
  color: var(--au-text);
  font-size: 14px;
}

input.redeem-input::placeholder {
  color: var(--au-text-3);
}

input.redeem-input:focus {
  border-color: var(--au-primary);
  outline: none;
  box-shadow: 0 0 12px var(--au-primary-border);
}

button.redeem-submit {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 44px;
  padding: 0 16px;
  background: var(--au-primary);
  color: var(--au-on-primary);
  border: none;
  border-radius: var(--au-r-md);
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  white-space: nowrap;
  flex-shrink: 0;
}

button.redeem-submit:hover {
  background: var(--au-primary-strong);
  box-shadow: 0 0 12px var(--au-primary-border);
}

button.redeem-submit:disabled {
  opacity: .5;
}

button.redeem-submit:active {
  transform: scale(.97);
}

/* ===== Preview / coupon / note ===== */
.redeem-preview {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 13px;
  color: var(--au-text-2);
}

.redeem-preview svg {
  width: 14px;
  height: 14px;
  color: var(--au-primary);
}

.redeem-preview strong {
  color: var(--au-text);
}

button.redeem-confirm {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 36px;
  padding: 0 16px;
  background: var(--au-primary);
  color: var(--au-on-primary);
  border: none;
  border-radius: var(--au-r-md);
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  white-space: nowrap;
}

button.redeem-confirm:hover {
  background: var(--au-primary-strong);
}

button.redeem-confirm:disabled {
  opacity: .5;
}

button.redeem-confirm:active {
  transform: scale(.97);
}

.redeem-coupon {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 13px;
  color: var(--au-text-2);
}

.redeem-coupon svg {
  width: 14px;
  height: 14px;
  color: var(--au-primary);
}

.redeem-coupon strong {
  color: var(--au-text);
}

button.redeem-clear {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 36px;
  padding: 0 12px;
  background: transparent;
  border: 1px solid var(--au-border-strong);
  color: var(--au-text-2);
  border-radius: var(--au-r-md);
  font-size: 13px;
  cursor: pointer;
}

button.redeem-clear:hover {
  border-color: var(--au-primary);
  color: var(--au-text);
}

button.redeem-clear svg {
  width: 13px;
  height: 13px;
}

.redeem-note {
  font-size: 12px;
  color: var(--au-text-3);
  margin: 0;
}

.redeem-note.warn {
  color: var(--au-warning);
}

/* ===== Skeleton sweep shimmer ===== */
.store-skeleton {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.store-skel-row {
  display: flex;
  align-items: center;
  padding: 16px;
  border-radius: var(--au-r-lg);
  background: var(--au-surface-2);
}

.store-skel-points,
.store-skel-price {
  background: linear-gradient(90deg, var(--au-track) 25%, var(--au-primary-soft) 50%, var(--au-track) 75%);
  background-size: 200% 100%;
  animation: store-shimmer 1.2s linear infinite;
}

.store-skel-points {
  width: 120px;
  height: 22px;
  border-radius: 4px;
}

.store-skel-price {
  width: 80px;
  height: 18px;
  border-radius: 4px;
  margin-left: auto;
}

@keyframes store-shimmer {
  from { background-position: 200% 0; }
  to { background-position: -200% 0; }
}

/* ===== Empty state ===== */
.store-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 32px 16px;
  color: var(--au-text-3);
  font-size: 13px;
  text-align: center;
}

.store-empty svg {
  width: 28px;
  height: 28px;
  opacity: .5;
}

/* ===== Spinner ===== */
.store-page .spinner {
  display: inline-block;
  width: 14px;
  height: 14px;
  flex-shrink: 0;
  border-radius: 50%;
  border: 2px solid var(--au-border-strong);
  border-top-color: var(--au-primary);
  animation: store-spin .7s linear infinite;
  vertical-align: -2px;
}

@keyframes store-spin {
  to { transform: rotate(360deg); }
}

/* ===== Mobile ===== */
@media (max-width: 768px) {
  .store-page {
    padding: 16px;
    padding-bottom: calc(16px + var(--au-dock-space, 62px));
    gap: 24px;
  }

  .store-hero {
    grid-template-columns: 1fr;
    gap: 16px;
    padding: 18px 20px;
  }

  .sh-divider {
    height: 1px;
    width: 100%;
  }

  a.sh-num .num {
    font-size: 34px;
  }

  .store-cards {
    grid-template-columns: 1fr;
  }

  .store-card {
    padding: 20px;
  }

  .pkg-row {
    padding: 16px;
  }

  .plan-actions button {
    flex: 1;
  }

  .pkg-name {
    max-width: 40vw;
  }
}

.store-tg {
  margin: 0;
}
</style>