import { ref, computed } from 'vue'
import { useUserStore } from '@/stores/user'
import { homeApi, type HomeSummary } from '@/api/economy'
import { isExpiringSoon, type MySubscription, type WatchStats } from '@/api'
import { Crown, Zap, Clapperboard } from 'lucide-vue-next'
import type { AssetCard } from '@/components/panel/TicketCard.vue'

/** R28：ISO 日期 "2026-11-01" → 人话 "11月1日"；非日期原文原样返回 */
function fmtMonthDay(isoDate: string): string {
  const m = isoDate.match(/^\d{4}-(\d{1,2})-(\d{1,2})/)
  if (!m) return isoDate
  return `${Number(m[1])}月${Number(m[2])}日`
}

/**
 * 电影票卡数据（原 HomeView.vue 票根卡逻辑，HomeView 删除后迁至此处）
 *
 * 两行布局：第一行能量 + 会员并排（能量 / 会员），第二行票根行里带一行观影小字。
 * 数据源是首页聚合接口 /api/user/home/summary。
 * 快照沿用 'aetrix:home-summary:v1'（与旧首页同一份），老用户切到发现页后
 * 首屏直接用缓存渲染，不闪骨架。
 */
export function useTicketCard() {
  const userStore = useUserStore()

  const loading = ref(true)
  const loadError = ref(false)
  const subscriptions = ref<MySubscription[]>([])
  const stats = ref<WatchStats | null>(null)

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

  // 票根卡的三格：能量 / 会员 / 观影数据
  const assetCards = computed((): AssetCard[] => {
    const st = stats.value

    // 观影数据的大数字：≥1 小时按小时、不足 1 小时按分钟（别让新用户一上来就看到「0」）
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

    const memberCard = isMember.value && activeSub.value
      ? {
          value: String(activeSub.value.days_left),
          unit: '天',
          progress: memberProgress.value,
          desc: `${activeSub.value.plan_name} · ${fmtMonthDay(activeSub.value.end_date?.slice(0, 10) || '')}到期`,
          note: expiringSoon.value ? '续费后新时长在到期日之后叠加' : '会员权益全库通用',
          short: expiringSoon.value ? '即将到期' : '',
          footer: '续费',
          badge: expiringSoon.value ? '临期' : '生效中',
          hot: expiringSoon.value,
        }
      : isFreeRealm.value
        ? {
            value: '免费',
            unit: '',
            progress: null,
            desc: userStore.realmNote || '公益服开放中，无需订阅即可观看全库内容',
            note: '本服不需要会员',
            short: '公益服',
            footer: '查看套餐',
            badge: null,
            hot: false,
          }
        : {
            value: '未开通',
            unit: '',
            progress: null,
            desc: gateMessage.value || '开通会员后可无限观看全部影视内容',
            note: '开通后解锁全库',
            short: '未开通',
            footer: '立即开通',
            badge: null,
            hot: false,
          }

    return [
      {
        key: 'points',
        to: '/game/ledger',
        icon: Zap,
        title: '能量',
        value: quickStats.value.balance !== null ? quickStats.value.balance.toLocaleString() : '—',
        unit: '',
        progress: null,
        desc: quickStats.value.checkinEnabled ? '报到、邀请与核销都能攒能量' : '邀请与核销都能攒能量',
        note: !quickStats.value.checkinEnabled
          ? ''
          : quickStats.value.checkedToday
            ? (quickStats.value.streak ? `今日已报到 · 连续 ${quickStats.value.streak} 天` : '今日已报到 · 明天再来')
            : '今天还没报到',
        // 窄屏票面只留一行 ≤6 字的状态（长说明在宽屏才显示）
        short: !quickStats.value.checkinEnabled
          ? ''
          : quickStats.value.checkedToday
            ? (quickStats.value.streak ? `连报 ${quickStats.value.streak} 天` : '今日已报到')
            : '今日未报到',
        footer: '查看明细',
        badge: !quickStats.value.checkinEnabled ? null : (quickStats.value.checkedToday ? null : '今日未报到'),
        // 未签时徽章走警示色（hot），与会员临期同一套提醒语言
        hot: quickStats.value.checkinEnabled && !quickStats.value.checkedToday,
      },
      {
        key: 'member',
        to: '/store',
        icon: Crown,
        title: '会员',
        ...memberCard,
      },
      {
        key: 'watch',
        to: '',
        icon: Clapperboard,
        title: '观影数据',
        value: watchValue,
        unit: watchUnit,
        progress: null,
        desc: `播放 ${st ? st.total_plays : '—'} 次 · 看过 ${st ? st.watched_items : '—'} 部`,
        note: '在 Infuse 等客户端继续观影',
        short: st ? `播放 ${st.total_plays} 次` : '',
        footer: '',
        badge: null,
        hot: false,
      },
    ]
  })

  const memberStat = computed(() => assetCards.value.find((c) => c.key === 'member') || null)
  const memberFooter = computed(() => memberStat.value?.footer || '查看套餐')
  const planName = computed(() => activeSub.value?.plan_name || '')

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
      // 能量 / 报到 / 会员 / 观影统计一次取回
      const data = await homeApi.summary()
      const memberFailed = data.subscriptions === null
      applySummary(data)
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
    assetCards,
    memberFooter,
    planName,
    load,
    hasLoaded,
  }
}
