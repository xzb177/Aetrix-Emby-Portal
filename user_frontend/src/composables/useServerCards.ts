/**
 * useServerCards —— Aetrix 用户端「服务卡」列表
 *
 * 数据来源（真实接口，勿改名）：
 * - `embyApi.getAccountCard()`  GET /api/user/emby/server → `{ realms?: AccountRealmCard[] }`
 *   （realms 已由后端按 sort_order 排序，前端不再排序）
 * - `homeApi.summary()`        积分 / 签到 / 订阅 / 统计
 * - `vitalityApi.status()`     能量值（失败时静默降级为 null）
 *
 * 说明：AccountRealmCard 不含 start_date，进度条数据无法计算，`progress` 恒为 null，
 *      由组件按 null 隐藏进度条，前端不编造数据。
 */
import { computed, ref } from 'vue'
import { embyApi, isExpiringSoon } from '@/api'
import type { AccountRealmCard, MySubscription } from '@/api'
import { homeApi, vitalityApi } from '@/api/economy'

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
  state: 'active' | 'expiring' | 'unsubscribed' | 'free_open'
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

/**
 * 把 `2025-11-06` / `2025-11-06T12:00:00Z` 之类的日期转成「11月6日」人话文案。
 * 无法解析时返回 null（不编造）。
 */
function toHumanDate(endDate: string | null | undefined): string | null {
  if (!endDate) return null
  const matched = /^\d{4}-(\d{1,2})-(\d{1,2})/.exec(endDate)
  if (!matched) return null
  return `${Number(matched[1])}月${Number(matched[2])}日`
}

/** 汇总 homeApi.summary / vitalityApi.status 的公共字段 */
function buildShared(summary: HomeSummary | null, vitality: VitalityStatus | null): SharedFields {
  const balanceValue = summary?.points?.balance
  const vitalityValue = vitality?.vitality
  const vitalityMax = vitality?.max
  const vitalityLimit = vitality?.limit_threshold

  return {
    // 积分余额：points.balance?.toLocaleString() ?? '—'
    balance: typeof balanceValue === 'number' ? balanceValue.toLocaleString() : '—',
    // 能量：有 max 用 `${v}/${max}`，只有 vitality 用 String(v)，都没有用 '—'
    vitalityText:
      vitalityValue != null
        ? vitalityMax != null
          ? `${vitalityValue}/${vitalityMax}`
          : String(vitalityValue)
        : '—',
    hasVitality: vitality != null,
    isVitalityLow: vitalityValue != null && vitalityLimit != null && vitalityValue <= vitalityLimit,
    checkinEnabled: summary?.checkin?.enabled ?? false,
    checkedToday: summary?.checkin?.checked_today ?? false,
    streak: summary?.checkin?.streak ?? null,
  }
}

/**
 * 会员进度（已用占比）：按订阅自己的 start_date → end_date 算；
 * 口径与 useTicketCard 的 memberProgress 一致，组件层做 100-x 转剩余方向。
 */
function memberProgressFromSub(sub: MySubscription): number {
  const start = new Date(sub.start_date).getTime()
  const end = new Date(sub.end_date).getTime()
  if (!start || !end || end <= start) return 0
  const total = end - start
  const used = Math.min(Math.max(Date.now() - start, 0), total)
  return Math.max(1, Math.min(100, Math.round((used / total) * 100)))
}

/** 付费服卡片 */
function buildPaidCard(
  realm: AccountRealmCard,
  shared: SharedFields,
  sub: MySubscription | null,
): ServerCardItem {
  const endDate = realm.end_date ?? null
  const daysLeft = endDate
    ? Math.ceil((new Date(endDate).getTime() - Date.now()) / DAY_MS)
    : null
  const subscribed = !!realm.subscribed
  const planName = realm.plan_name ?? ''
  const endText = toHumanDate(endDate)

  let state: ServerCardItem['state']
  let badge: string | null
  let footer: string
  let to: string

  if (!subscribed || (daysLeft !== null && daysLeft <= 0)) {
    // 未订阅 / 已过期：引导开通
    state = 'unsubscribed'
    badge = null
    footer = '立即开通'
    to = '/store'
  } else if (
    daysLeft !== null &&
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
    badge = '临期'
    footer = '续费'
    to = '/store'
  } else {
    state = 'active'
    badge = '生效中'
    footer = '续费'
    to = '/store'
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
    progress: sub ? memberProgressFromSub(sub) : null,
    badge,
    state,
    footer,
    to,
    // 有到期时间：`{planName} · {人话到期}到期`；否则用左底通用文案
    desc: endText ? `${planName} · ${endText}到期` : '会员权益全库通用',
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
    badge: '免费开放',
    state: 'free_open',
    footer: '去补给站',
    to: '/shop',
    desc: '能量在补给站和账本里用',
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

/** realms 缺失 / 空数组时的两张降级卡 */
function buildLegacyCards(shared: SharedFields): ServerCardItem[] {
  const paid: ServerCardItem = {
    id: -1,
    name: '付费服',
    slug: '',
    accessMode: 'paid',
    subscribed: false,
    planName: '',
    endDate: null,
    daysLeft: null,
    progress: null,
    badge: null,
    state: 'unsubscribed',
    footer: '立即开通',
    to: '/store',
    desc: '开通会员后可无限观看全部影视内容',
    expiringSoon: false,
    balance: '',
    vitalityText: '',
    hasVitality: false,
    isVitalityLow: false,
    checkinEnabled: false,
    checkedToday: false,
    streak: null,
  }

  const free: ServerCardItem = {
    id: -2,
    name: '公益服',
    slug: '',
    accessMode: 'free',
    subscribed: true, // 公益服对所有人开放
    planName: '',
    endDate: null,
    daysLeft: null,
    progress: null,
    badge: '免费开放',
    state: 'free_open',
    footer: '去补给站',
    to: '/shop',
    desc: '能量在补给站和账本里用',
    expiringSoon: false,
    ...shared,
  }

  return [paid, free]
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
      totalPlays: st && typeof st.total_plays === 'number' ? String(st.total_plays) : '—',
      watchedItems: st && typeof st.watched_items === 'number' ? String(st.watched_items) : '—',
    }
  })

  /**
   * 并发拉取服务卡 / 摘要 / 能量三个接口。
   * - getAccountCard 失败 → servers = [] 且 loadError = true，不抛错
   * - vitalityApi.status 失败 → 静默降级为 null
   */
  async function load(): Promise<void> {
    loading.value = true
    loadError.value = false

    try {
      const [cardRes, summaryRes, vitalityRes] = await Promise.all([
        embyApi.getAccountCard().catch((): null => null),
        homeApi.summary().catch((): null => null),
        vitalityApi.status().catch((): null => null),
      ])

      const shared = buildShared(summaryRes, vitalityRes)
      if (summaryRes?.stats) stats.value = summaryRes.stats

      if (!cardRes) {
        // 服务卡接口失败：降级为空列表 + 错误标记
        loadError.value = true
        servers.value = []
        return
      }

      const realms: AccountRealmCard[] = Array.isArray(cardRes.realms) ? cardRes.realms : []
      const subs: MySubscription[] = Array.isArray(summaryRes?.subscriptions)
        ? summaryRes.subscriptions
        : []

      servers.value =
        realms.length > 0
          ? realms.map((realm: AccountRealmCard) => buildRealmCard(realm, shared, subs)) // 后端已按 sort_order 排序
          : buildLegacyCards(shared)
    } finally {
      loading.value = false
      hasLoaded.value = true
    }
  }

  return { servers, load, loading, loadError, hasLoaded, watchStrip }
}
