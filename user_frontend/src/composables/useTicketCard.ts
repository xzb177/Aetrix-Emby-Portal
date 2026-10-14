import { ref, computed } from 'vue'
import { useUserStore } from '@/stores/user'
import { homeApi, type HomeSummary, vitalityApi, type VitalityStatus } from '@/api/economy'
import { isExpiringSoon, type MySubscription, type WatchStats } from '@/api'
import { Crown, Zap, Clapperboard } from 'lucide-vue-next'

export interface MemberCardInfo {
  value: string
  unit: string
  progress: number | null
  planName: string
  expiryDate: string
  desc: string
  badge: string | null
  hot: boolean
  expiringSoon: boolean
  footer: string
  to: string
  state: 'active' | 'expiring' | 'free_realm' | 'unsubscribed'
}

export interface EnergyCardInfo {
  balance: string
  vitality: number | null
  vitalityMax: number | null
  vitalityText: string
  vitalityPct: number
  isVitalityLow: boolean
  hasVitality: boolean
  badge: string | null
  hot: boolean
  checkinEnabled: boolean
  checkedToday: boolean
  streak: number | null
  cardTo: string
  buttonText: string
  buttonTo: string
}

export interface WatchStripInfo {
  watchValue: string
  watchUnit: string
  watchTime: string
  totalPlays: string
  watchedItems: string
}

export interface AssetCard {
  key: string
  to: string
  icon: unknown
  title: string
  value: string
  unit: string
  progress: number | null
  desc: string
  note: string
  short: string
  footer: string
  badge: string | null
  hot: boolean
}

/** R28：ISO 日期 "2026-11-01" → 人话 "11月1日"；非日期原文原样返回 */
function fmtMonthDay(isoDate: string): string {
  const m = isoDate.match(/^\d{4}-(\d{1,2})-(\d{1,2})/)
  if (!m) return isoDate
  return `${Number(m[1])}月${Number(m[2])}日`
}

/**
 * 首页两张卡（会员卡 + 能量卡）与一条观影数据小条的数据逻辑
 *
 * 数据源：
 * - 首页聚合接口 /api/user/home/summary（能量 / 报到 / 会员 / 观影统计）
 * - 体力接口 /api/user/economy/vitality（vitalityApi.status()，失败容错占位）
 * 快照沿用 'aetrix:home-summary:v1'，回访用户首屏直接用缓存渲染，不闪骨架。
 */
export function useTicketCard() {
  const userStore = useUserStore()

  const loading = ref(true)
  const loadError = ref(false)
  const subscriptions = ref<MySubscription[]>([])
  const stats = ref<WatchStats | null>(null)
  const vitalityStatus = ref<VitalityStatus | null>(null)

  // 经济速览（账号速览条数据）
  const quickStats = ref({
    balance: null as number | null,
    streak: null as number | null,
    checkedToday: false,
    invited: null as number | null,
    // 报到功能开关（管理端可配）：关闭时隐藏报到相关文案
    checkinEnabled: true,
  })

  const activeSub = computed(
    () => subscriptions.value.find((s) => s.status === 'active' && s.days_left > 0) || null,
  )

  // 会员状态卡：会员有效期内显示套餐与剩余天数；无会员时展示付费墙引导
  const isMember = computed(() => !!activeSub.value)
  // 临期提醒：与后台「到期前 7 天提醒」同一口径（见 backend/reminders.py）
  const expiringSoon = computed(() => isExpiringSoon(activeSub.value))
  const gateOn = computed(() => !!userStore.user?.subscription_required)
  const gateMessage = computed(() =>
    gateOn.value && !isMember.value
      ? '当前账号没有生效中的会员，开通后即可播放全库内容'
      : '',
  )
  // 公益服：免费开放，不需要会员，不再推销会员
  const isFreeRealm = computed(() => userStore.isFreeRealm)
  // 套餐周期已过多少：按会员自己的 start_date → end_date 算
  const memberProgress = computed(() => {
    const sub = activeSub.value
    if (!sub) return 0
    const start = new Date(sub.start_date).getTime()
    const end = new Date(sub.end_date).getTime()
    if (!start || !end || end <= start) return 0
    const total = end - start
    const used = Math.min(Math.max(Date.now() - start, 0), total)
    return Math.max(1, Math.min(100, Math.round((used / total) * 100)))
  })

  // 1. 会员卡结构化数据
  const memberCard = computed((): MemberCardInfo => {
    if (isMember.value && activeSub.value) {
      const sub = activeSub.value
      const expiry = sub.end_date?.slice(0, 10) || ''
      const expiryHuman = expiry ? fmtMonthDay(expiry) : ''
      return {
        value: String(sub.days_left),
        unit: '天',
        progress: memberProgress.value,
        planName: sub.plan_name,
        expiryDate: expiryHuman,
        desc: `${sub.plan_name}${expiryHuman ? ` · ${expiryHuman}到期` : ''}`,
        badge: expiringSoon.value ? '临期' : '生效中',
        hot: expiringSoon.value,
        expiringSoon: expiringSoon.value,
        footer: '续费',
        to: '/store',
        state: expiringSoon.value ? 'expiring' : 'active',
      }
    }

    if (isFreeRealm.value) {
      return {
        value: '免费',
        unit: '',
        progress: null,
        planName: '',
        expiryDate: '',
        desc: userStore.realmNote || '公益服开放中，无需订阅即可观看全库内容',
        badge: '公益服',
        hot: false,
        expiringSoon: false,
        footer: '查看套餐',
        to: '/store',
        state: 'free_realm',
      }
    }

    return {
      value: '未开通',
      unit: '',
      progress: null,
      planName: '',
      expiryDate: '',
      desc: gateMessage.value || '开通会员后可无限观看全部影视内容',
      badge: null,
      hot: false,
      expiringSoon: false,
      footer: '立即开通',
      to: '/store',
      state: 'unsubscribed',
    }
  })

  // 体力数据与状态判定
  const vitality = computed(() => vitalityStatus.value?.vitality ?? null)
  const vitalityMax = computed(() => {
    const m = vitalityStatus.value?.max
    return typeof m === 'number' && m > 0 ? m : null
  })
  const isVitalityLow = computed(() => {
    if (!vitalityStatus.value) return false
    const v = vitalityStatus.value.vitality
    const threshold = vitalityStatus.value.limit_threshold
    return typeof threshold === 'number' && v <= threshold
  })
  const vitalityPct = computed(() => {
    const v = vitality.value
    const m = vitalityMax.value
    if (v === null || m === null || m <= 0) return 0
    return Math.min(100, Math.max(0, Math.round((v / m) * 100)))
  })
  const vitalityText = computed(() => {
    if (vitality.value !== null && vitalityMax.value !== null) {
      return `${vitality.value}/${vitalityMax.value}`
    }
    if (vitality.value !== null) {
      return String(vitality.value)
    }
    return '—'
  })

  // 2. 能量卡结构化数据
  const energyCard = computed((): EnergyCardInfo => {
    const qs = quickStats.value
    const balanceStr = qs.balance !== null ? qs.balance.toLocaleString() : '—'

    let badge: string | null = null
    let hot = false
    if (qs.checkinEnabled) {
      if (!qs.checkedToday) {
        badge = '今日未报到'
        hot = true
      } else if (qs.streak && qs.streak > 0) {
        badge = `连签 ${qs.streak} 天`
        hot = false
      } else {
        badge = '今日已报到'
        hot = false
      }
    }

    return {
      balance: balanceStr,
      vitality: vitality.value,
      vitalityMax: vitalityMax.value,
      vitalityText: vitalityText.value,
      vitalityPct: vitalityPct.value,
      isVitalityLow: isVitalityLow.value,
      hasVitality: vitalityStatus.value !== null,
      badge,
      hot,
      checkinEnabled: qs.checkinEnabled,
      checkedToday: qs.checkedToday,
      streak: qs.streak,
      cardTo: '/game/ledger',
      buttonText: '去补给站',
      buttonTo: '/shop',
    }
  })

  // 3. 观影数据小条结构化数据
  const watchStrip = computed((): WatchStripInfo => {
    const st = stats.value
    let watchValue = '—'
    let watchUnit = ''
    if (st && st.total_seconds > 0) {
      const hours = Math.floor(st.total_seconds / 3600)
      if (hours >= 1) {
        watchValue = String(hours)
        watchUnit = '小时'
      } else {
        watchValue = String(Math.max(1, Math.round(st.total_seconds / 60)))
        watchUnit = '分钟'
      }
    }
    const watchTime = watchValue !== '—' ? `${watchValue}${watchUnit}` : '—'
    const totalPlays = st && typeof st.total_plays === 'number' ? String(st.total_plays) : '—'
    const watchedItems = st && typeof st.watched_items === 'number' ? String(st.watched_items) : '—'

    return {
      watchValue,
      watchUnit,
      watchTime,
      totalPlays,
      watchedItems,
    }
  })

  // 兼容老调用方（assetCards）
  const assetCards = computed((): AssetCard[] => {
    const mc = memberCard.value
    const ec = energyCard.value
    const ws = watchStrip.value
    return [
      {
        key: 'points',
        to: ec.cardTo,
        icon: Zap,
        title: '能量',
        value: ec.balance,
        unit: '',
        progress: null,
        desc: ec.checkinEnabled ? '报到、邀请与核销都能攒能量' : '邀请与核销都能攒能量',
        note: !ec.checkinEnabled
          ? ''
          : ec.checkedToday
            ? (ec.streak ? `今日已报到 · 连续 ${ec.streak} 天` : '今日已报到 · 明天再来')
            : '今天还没报到',
        short: ec.badge || '',
        footer: '查看明细',
        badge: ec.badge,
        hot: ec.hot,
      },
      {
        key: 'member',
        to: mc.to,
        icon: Crown,
        title: '会员',
        value: mc.value,
        unit: mc.unit,
        progress: mc.progress,
        desc: mc.desc,
        note: mc.expiringSoon ? '续费后新时长在到期日之后叠加' : '会员权益全库通用',
        short: mc.expiringSoon ? '即将到期' : mc.badge || '',
        footer: mc.footer,
        badge: mc.badge,
        hot: mc.hot,
      },
      {
        key: 'watch',
        to: '',
        icon: Clapperboard,
        title: '观影数据',
        value: ws.watchValue,
        unit: ws.watchUnit,
        progress: null,
        desc: `播放 ${ws.totalPlays} 次 · 看过 ${ws.watchedItems} 部`,
        note: '在 Infuse 等客户端继续观影',
        short: ws.totalPlays !== '—' ? `播放 ${ws.totalPlays} 次` : '',
        footer: '',
        badge: null,
        hot: false,
      },
    ]
  })

  const memberFooter = computed(() => memberCard.value.footer)
  const planName = computed(() => memberCard.value.planName)

  // ---- 数据快照：变化以天为单位，快照让回访用户首屏 0 骨架 ----
  const SNAPSHOT_KEY = 'aetrix:home-summary:v1'
  const SNAPSHOT_MAX_AGE_MS = 24 * 60 * 60 * 1000

  interface HomeSnapshot {
    savedAt: number
    userId: number
    data: HomeSummary
  }

  function readSnapshot(userId: number): HomeSummary | null {
    try {
      const raw = localStorage.getItem(SNAPSHOT_KEY)
      if (!raw) return null
      const snap = JSON.parse(raw) as HomeSnapshot
      if (snap.userId !== userId) return null
      if (Date.now() - snap.savedAt > SNAPSHOT_MAX_AGE_MS) return null
      return snap.data
    } catch {
      return null
    }
  }

  function writeSnapshot(userId: number, data: HomeSummary) {
    try {
      const snap: HomeSnapshot = { savedAt: Date.now(), userId, data }
      localStorage.setItem(SNAPSHOT_KEY, JSON.stringify(snap))
    } catch {
      // 隐私模式 / 配额满：快照只是优化，拿不到就算了
    }
  }

  function applySummary(data: HomeSummary) {
    if (data.points) quickStats.value.balance = data.points.balance
    if (data.checkin) {
      quickStats.value.streak = data.checkin.streak
      quickStats.value.checkedToday = data.checkin.checked_today
      quickStats.value.checkinEnabled = data.checkin.enabled !== false
    }
    if (data.subscriptions) subscriptions.value = data.subscriptions
    if (data.stats) stats.value = data.stats
  }

  const hasLoaded = ref(false)
  async function load() {
    const userId = userStore.user?.id
    const snapshot = userId != null ? readSnapshot(userId) : null
    if (snapshot) {
      applySummary(snapshot)
      hasLoaded.value = true
      loading.value = false
    } else {
      loading.value = true
    }
    try {
      // 能量 / 报到 / 会员 / 观影统计一次取回；体力接口并发拉取，失败静默降级不阻断整卡渲染
      const [summaryRes, vitalityRes] = await Promise.all([
        homeApi.summary(),
        vitalityApi.status().catch((err): null => {
          console.warn('[useTicketCard] vitality load failed', err)
          return null
        }),
      ])
      const data = summaryRes
      const memberFailed = data.subscriptions === null
      applySummary(data)
      if (vitalityRes) {
        vitalityStatus.value = vitalityRes
      }
      if (Array.isArray(data.subscriptions)) subscriptions.value = data.subscriptions
      loadError.value = memberFailed
      if (userId != null) writeSnapshot(userId, data)
    } catch {
      if (snapshot) return // 有快照可显示时不打扰用户
      // R28：无快照且请求失败时只留页面错误态 + 重试按钮，不再额外弹 toast（避免双重提示）
      loadError.value = true
    } finally {
      loading.value = false
      hasLoaded.value = true
    }
  }

  return {
    loading,
    loadError,
    memberCard,
    energyCard,
    watchStrip,
    load,
    hasLoaded,
    // 兼容原字段
    assetCards,
    memberFooter,
    planName,
  }
}
