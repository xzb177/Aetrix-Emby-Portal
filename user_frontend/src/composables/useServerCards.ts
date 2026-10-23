/**
 * useServerCards —— Aetrix 用户端「服务卡」列表
 *
 * 数据来源（真实接口，勿改名）：
 * - `embyApi.getAccountCard()`  GET /api/user/emby/server → `{ realms?: AccountRealmCard[] }`
 *   （realms 已由后端按 sort_order 排序，前端不再排序）
 * - `homeApi.summary()`        积分 / 签到 / 订阅 / 统计
 * - `vitalityApi.status()`     能量值（失败时静默降级为 null）
 *
 * 说明：进度条按 home/summary 订阅明细的 start_date → end_date 计算已用占比；无该服 active 订阅明细时为 null，组件按 null 隐藏进度条，前端不编造数据。
 */
import { computed, ref } from 'vue'
import { embyApi, isExpiringSoon } from '@/api'
import type { AccountRealmCard, MySubscription } from '@/api'
import { homeApi, vitalityApi } from '@/api/economy'
import { subscriptionUsedPercent } from '@/utils/subscription'

const ROUTES = { store: '/store', shop: '/shop' } as const
const COPY = {
  badgeActive: '生效中', badgeExpiring: '临期', badgeExpired: '已过期', badgeFree: '免费开放',
  footerOpen: '立即开通', footerRenew: '续费', footerShop: '去补给站',
  descMember: '会员权益全库通用', descFree: '能量在补给站和账本里用',
} as const

/** 服务卡视图模型 */
export interface ServerCardItem {
  id: number
  name: string
  slug: string
  accessMode: 'paid' | 'free'
  subscribed: boolean
  planName: string
  endDate: string | null
  daysLeft: number | null
  progress: number | null
  badge: string | null
  state: 'active' | 'expiring' | 'expired' | 'unsubscribed' | 'free_open'
  footer: string
  to: string
  desc: string
  expiringSoon: boolean
  balance: string
  vitalityText: string
  hasVitality: boolean
  isVitalityLow: boolean
  checkinEnabled: boolean
  checkedToday: boolean
  streak: number | null
}

/** homeApi.summary() 的返回类型 */
type HomeSummary = Awaited<ReturnType<typeof homeApi.summary>>
/** vitalityApi.status() 的返回类型 */
type VitalityStatus = Awaited<ReturnType<typeof vitalityApi.status>>

/** 卡片公共字段（来自 homeApi.summary / vitalityApi.status） */
type SharedFields = Pick<
  ServerCardItem,
  | 'balance'
  | 'vitalityText'
  | 'hasVitality'
  | 'isVitalityLow'
  | 'checkinEnabled'
  | 'checkedToday'
  | 'streak'
>

const DAY_MS = 86_400_000

let serverCardsReqSeq = 0

/**
 * 把 `2025-11-06` / `2025-11-06T12:00:00Z` 之类的日期转成「11月6日」人话文案。
 * 无法解析时返回 null（不编造）。
 */
function toHumanDate(endDate: string | null | undefined): string | null {
  if (!endDate) return null
  const matched = /^\d{4}-(\d{1,2})-(\d{1,2})/.exec(endDate)
  if (!matched) return null
  const mo = Number(matched[1])
  const da = Number(matched[2])
  if (mo < 1 || mo > 12 || da < 1 || da > 31) return null
  return `${mo}月${da}日`
}

/**
 * 解析到期日：`YYYY-MM-DD` 按本地日历日当天 23:59:59.999 处理；
 * 其他格式回退到 `new Date(value).getTime()`，无法解析返回 null。
 */
function parseEndOfDay(value: string | null | undefined): number | null {
  if (!value) return null
  const matched = /^(\d{4})-(\d{1,2})-(\d{1,2})/.exec(value)
  if (matched) {
    const y = Number(matched[1])
    const m = Number(matched[2])
    const d = Number(matched[3])
    if (m < 1 || m > 12 || d < 1 || d > 31) return null
    return new Date(y, m - 1, d, 23, 59, 59, 999).getTime()
  }
  const ts = new Date(value).getTime()
  return Number.isNaN(ts) ? null : ts
}

/** 汇总 homeApi.summary / vitalityApi.status 的公共字段 */
function buildShared(summary: HomeSummary | null, vitality: VitalityStatus | null): SharedFields {
  const balanceValue = summary?.points?.balance
  const vitalityValue = vitality?.vitality
  const vitalityMax = vitality?.max
  const vitalityLimit = vitality?.limit_threshold

  return {
    // 积分余额：points.balance?.toLocaleString() ?? '—'
    balance: Number.isFinite(balanceValue) ? (balanceValue as number).toLocaleString() : '—',
    // 能量：有 max 用 `${v}/${max}`，只有 vitality 用 String(v)，都没有用 '—'
    vitalityText:
      vitalityValue != null
        ? vitalityMax != null
          ? `${vitalityValue}/${vitalityMax}`
          : String(vitalityValue)
        : '—',
    hasVitality: vitalityValue != null,
    isVitalityLow: vitalityValue != null && vitalityLimit != null && vitalityValue <= vitalityLimit,
    checkinEnabled: summary?.checkin?.enabled ?? false,
    checkedToday: summary?.checkin?.checked_today ?? false,
    streak: summary?.checkin?.streak ?? null,
  }
}

/** 付费服卡片 */
function buildPaidCard(
  realm: AccountRealmCard,
  shared: SharedFields,
  sub: MySubscription | null,
): ServerCardItem {
  const endDate = realm.end_date ?? null
  const endTs = parseEndOfDay(endDate)
  // 剩余天数以后端下发的 days_left 为准（与「我的」页同口径）；后端没给才回退旧自算
  // （旧自算「补到当天 23:59:59 + 向上取整」比后端多 1~2 天，2026-10-23 统一口径）
  const legacyDaysLeft = endTs === null ? null : Math.ceil((endTs - Date.now()) / DAY_MS)
  const daysLeft = typeof realm.days_left === 'number' ? realm.days_left : legacyDaysLeft
  const subscribed = !!realm.subscribed
  const planName = realm.plan_name ?? ''
  const endText = toHumanDate(endDate)

  let state: ServerCardItem['state']
  let badge: string | null
  let footer: string
  let to: string

  if (
    subscribed &&
    daysLeft !== null &&
    daysLeft <= 0 &&
    // 后端 days_left=0 是「今天到期、仍可用」，不能误判已过期；须到期日当天也已过完才算过期
    endTs !== null &&
    endTs <= Date.now()
  ) {
    // 已过期：引导续费
    state = 'expired'
    badge = COPY.badgeExpired
    footer = COPY.footerRenew
    to = ROUTES.store
  } else if (!subscribed) {
    // 未订阅：引导开通
    state = 'unsubscribed'
    badge = null
    footer = COPY.footerOpen
    to = ROUTES.store
  } else if (
    subscribed &&
    daysLeft !== null &&
    daysLeft > 0 &&
    // isExpiringSoon 实际只读取 status 与 days_left，此处两者均为真实值（start_date/status 其余字段为占位）
    isExpiringSoon({
      id: realm.id,
      plan_name: planName,
      start_date: '',
      end_date: endDate ?? '',
      status: 'active',
      auto_renew: false,
      days_left: daysLeft,
    })
  ) {
    // 7 天口径内临期
    state = 'expiring'
    badge = COPY.badgeExpiring
    footer = COPY.footerRenew
    to = ROUTES.store
  } else {
    state = 'active'
    badge = COPY.badgeActive
    footer = COPY.footerRenew
    to = ROUTES.store
  }

  return {
    id: realm.id,
    name: realm.name,
    slug: realm.slug,
    accessMode: 'paid',
    subscribed,
    planName,
    endDate,
    daysLeft,
    // 有该服的订阅明细时按 start→end 算已用占比；没有则为 null（组件按 null 隐藏进度条）
    // 有该服的订阅明细时按 start→end 算已用占比；没有则为 null（组件按 null 隐藏进度条）。口径统一见 @/utils/subscription
    progress: sub ? subscriptionUsedPercent(sub.start_date, sub.end_date) : null,
    badge,
    state,
    footer,
    to,
    // 有到期时间：`{planName} · {人话到期}到期`；否则用左底通用文案
    desc: endText ? (planName ? `${planName} · ${endText}到期` : `${endText}到期`) : COPY.descMember,
    expiringSoon: state === 'expiring',
    ...shared,
  }
}

/** 公益服（免费）卡片 */
function buildFreeCard(realm: AccountRealmCard, shared: SharedFields): ServerCardItem {
  return {
    id: realm.id,
    name: realm.name,
    slug: realm.slug,
    accessMode: 'free',
    subscribed: !!realm.subscribed,
    planName: realm.plan_name ?? '',
    endDate: realm.end_date ?? null,
    daysLeft: null,
    progress: null,
    badge: COPY.badgeFree,
    state: 'free_open',
    footer: COPY.footerShop,
    to: ROUTES.shop,
    desc: COPY.descFree,
    expiringSoon: false,
    ...shared,
  }
}

/** 按 access_mode 分发 */
function buildRealmCard(
  realm: AccountRealmCard,
  shared: SharedFields,
  subs: MySubscription[],
): ServerCardItem {
  if (realm.access_mode === 'free') return buildFreeCard(realm, shared)
  // 用 home/summary 里的订阅明细按 realm_id 对上该服，拿 start_date 算进度
  const sub = subs.find((s) => s.realm_id === realm.id && s.status === 'active') ?? null
  return buildPaidCard(realm, shared, sub)
}

/** 观影数据小条（沿用 useTicketCard 的口径） */
export interface ServerWatchStrip {
  watchTime: string
  totalPlays: string
  watchedItems: string
}

export function useServerCards() {
  /** 服务卡列表 */
  const servers = ref<ServerCardItem[]>([])
  /** 加载中 */
  const loading = ref(false)
  /** 服务卡接口失败标记（失败时 servers 为空数组，不抛错） */
  const loadError = ref(false)
  /** 是否已完成过一次加载 */
  const hasLoaded = ref(false)
  /** 观影统计（homeApi.summary().stats） */
  const stats = ref<{ total_seconds: number; total_plays: number; watched_items: number } | null>(null)

  const watchStrip = computed((): ServerWatchStrip => {
    const st = stats.value
    let watchTime = '—'
    if (st && st.total_seconds > 0) {
      const hours = Math.floor(st.total_seconds / 3600)
      watchTime = hours >= 1 ? `${hours}小时` : `${Math.max(1, Math.round(st.total_seconds / 60))}分钟`
    }
    return {
      watchTime,
      totalPlays: st && Number.isFinite(st.total_plays) ? String(st.total_plays) : '—',
      watchedItems: st && Number.isFinite(st.watched_items) ? String(st.watched_items) : '—',
    }
  })

  /**
   * 并发拉取服务卡 / 摘要 / 能量三个接口。
   * - getAccountCard 失败 → servers = [] 且 loadError = true，不抛错
   * - vitalityApi.status 失败 → 静默降级为 null
   */
  async function load(): Promise<void> {
    const myReq = ++serverCardsReqSeq
    loading.value = true
    loadError.value = false

    try {
      const [cardRes, summaryRes, vitalityRes] = await Promise.all([
        embyApi.getAccountCard().catch((): null => null),
        homeApi.summary().catch((): null => null),
        vitalityApi.status().catch((): null => null),
      ])

      const shared = buildShared(summaryRes, vitalityRes)
      if (myReq !== serverCardsReqSeq) return
      stats.value = summaryRes?.stats ?? null

      if (!cardRes) {
        if (myReq !== serverCardsReqSeq) return
        // 服务卡接口失败：降级为空列表 + 错误标记
        loadError.value = true
        servers.value = []
        return
      }

      const realms: AccountRealmCard[] = Array.isArray(cardRes.realms) ? cardRes.realms : []
      const subs: MySubscription[] = Array.isArray(summaryRes?.subscriptions)
        ? summaryRes.subscriptions
        : []

      if (myReq !== serverCardsReqSeq) return
      servers.value =
        realms.length > 0
          ? realms.map((realm) => buildRealmCard(realm, shared, subs)) // 后端已按 sort_order 排序
          : []
    } finally {
      if (myReq !== serverCardsReqSeq) return
      loading.value = false
      hasLoaded.value = true
    }
  }

  return { servers, load, loading, loadError, hasLoaded, watchStrip }
}
