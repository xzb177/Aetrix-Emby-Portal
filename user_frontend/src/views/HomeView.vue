<script setup lang="ts">
/**
 * 首页 — 服务台口径的个人门户（不再是"内容货架"）
 *
 * 背景：用户实际在 Infuse / Forward 等第三方客户端里看片，web 端首页的任务不是
 * "让人找片"，而是"让人看上片"——新用户引导、会员状态、求片、帮助。
 *
 * 布局：Hero 双栏（左：按用户状态分三种形态；右：会员状态卡）
 * → 新手任务（有未完成步骤时才出现）→ 账号速览条（积分 / 签到 / 邀请）
 * → 我的面板（观影数据 / 进行中的事项 / 正在播放）→ 最近上新（上新公告）
 * → 帮助中心（连接播放器 / 求片 / 联系客服）。
 *
 * Hero 左栏三形态：未开通显示"三步看片"指引（开通→下载客户端→一键导入），
 * 已开通/公益服显示服务台快捷入口（求片/连接播放器）。看片在客户端完成。
 * （早先这里写的是「首页不造继续观看」；暗房影院改版后按设计稿补回了一条精简版，
 * 见文件末尾「继续观看」的说明。）
 *
 * v2.10.3：底部那张「消息中心」卡去掉——它和顶栏带角标的音铃列的是同一批未读，
 * 同一件事在首页出现两遍。站内消息统一由顶栏铃铛承担（角标 + 点开预览 + 落到消息
 * 中心），首页不再单独占一条高度，也少一组首页请求。
 *
 * 排序口径：客户端（Infuse / Forward …）已经做得很好的事（继续观看、收藏、
 * 完整片库浏览）在首页只保留一条、且排在后面；门户独有的价值——跟着更新追新、
 * 库里没有就求片、账号与经济状态——放在前面。功能入口统一由顶栏导航承担。
 *
 * v2.10.1：首屏与尾部的两处布局收一收——会员卡里的进度条改成按订阅的真实周期
 * （start_date → end_date）算，不再用「剩余天数猜一个分母」；「账号与支持」的两张卡
 * 宽屏并排成两列，不再一前一后各占一条高度。
 *
 * v2.10.2：预览不再「看起来重复」——同名未读合并成一条并标条数，公告已作为未读
 * 站内信在列时不再重复列（口径在顶栏铃铛里，见 AppHeader.loadMsgPreview）。
 *
 * v2.6.26：站内消息不置顶、也不挤进账号速览条（挤进去会把「数据条」变成混合体）：
 * 速览条只留「账号与经济」（积分 / 签到 / 邀请），消息由顶栏带角标的音铃承担。
 *
 * v2.34.0：速览条下面补上「我的面板」——观影数据（累计时长 / 播放次数 / 看过影片）、
 * 进行中的事项（我的求片 / 我的工单，带数量与入口）、以及**只有本账号真在播时才出现**的
 * 「正在播放」（带一键结束）。这三件都是门户独有、客户端给不了的：客户端只知道「这台机器在放
 * 什么」，不知道账号名下还有哪些设备在放、求片处理到哪一步、工单有没有人回。
 *
 * 面板只做「一眼看到状态 + 点进去办事」：完整的会话清单与设备管理仍在个人中心（控制面板），
 * 与顶栏铃铛 / 消息中心的分工一致——同一个语义，摘要在一处、全量在另一处，不会两处都铺全。
 *
 * v2.42.1（借鉴纸片人控制台的设计语言）：首页改版为「资产仪表盘」口径——
 * 顶栏常驻积分 / 订阅资产 pill；hero 收敛成一张带装饰光晕的欢迎卡；
 * 原账号速览条升级为四段式资产卡（积分 / 订阅 / 观影数据），每种资产一个固定
 * 功能色（青 / 金 / 紫），从 pill → 卡片图标 / 数字 / CTA 全链路同色；
 * hero 右侧的会员卡并入订阅资产卡（进度条按真实周期算的口径不变）。
 * ≤768px 由底部导航坞（AppDock）承担主导航，卡片单列。
 *
 * v2.42.2：参考的纸片人 dashboard 只有账户资产与服务入口、没有媒体浏览，
 * 首页据此砍掉「最近上新」海报墙与空态卡——媒体浏览统一走媒体库独立页
 * （顶栏 / 底部坞的「媒体库」入口不变）。首页收敛为：欢迎卡 → 说明条幅
 * （置顶公告 / 公益服 / 未开通三态）→ 资产卡 → 新手任务 → 面板 → 帮助。
 * 同时接入双主题（跟随系统 / 白日 / 黑暗，见 useTheme.ts）：组件全部消费
 * --au-* 原料（在浅色下有独立调色），性能上移动端不启用卡片级 backdrop-blur、
 * 脉动与进度条动画只走 opacity/transform。
 *
 * 「暗房影院」改版：首页从上到下是——
 *   ① 幕布 Hero：整幅背景图（取「追新日历」近 7 天入库里第一张带背景图的条目，
 *      没有就是一块暖黑渐变），左下角琥珀眉题 + 衬线问候 + 状态行 + 唯一一个琥珀主按钮；
 *   ② 票根卡：原三张资产卡（积分 / 订阅 / 观影数据）合成一张电影票，右侧票根是续费入口；
 *   ③ 今日入库：近 7 天入库的竖版海报横滑（同一份日历数据，点击走 Rex deep link）；
 *   ③b 继续观看：最多 3 条「有进度、没看完」，最近播放的在前；
 *   ④ 正在播放 → ⑤ 进行中 → ⑥ 帮助中心（纯文字列表）。
 * 数据口径一律沿用原有绑定，只换视觉；新增的只有一次追新日历请求（失败静默，不影响首屏）。
 *
 * 继续观看（按设计稿补回）：不是客户端首页那种货架，只是「上次看到哪、在哪台设备上看的」
 * 一眼状态——每行 16:9 剧照 + 《片名》第 N 集 + 「客户端 · 设备」+ 琥珀进度条。
 * 数据走门户的 /api/user/emby/resume（不是三方客户端用的 /Items/Resume：那边按口径
 * 把单集整个排除了，剧看到一半在那边看不到）：单集已按剧聚合，客户端 / 设备取自
 * 该条目最近一次播放会话。点击与「本周入库」一致走 Rex deep link；没有条目整段不渲染，
 * 请求失败也静默。首页是 keep-alive，切回来时后台刷新一次（刚在客户端看完回来要对得上）。
 */
import { ref, computed, onMounted, onActivated, watch } from 'vue'
import { RouterLink } from 'vue-router'
// 观影数据卡无落地页（媒体库已下线），同模板里 RouterLink 与 div 二选一
import { useUserStore } from '@/stores/user'
import {
  isExpiringSoon, embyApi,
  type MySubscription, type WatchStats, type MyPlaybackSession, type Announcement,
  type PortalResumeItem,
} from '@/api'
import { useToast } from '@/composables/useToast'
import { toastSystem } from '@/constants/toastMessages'
import { toUserMessage } from '@/utils/toUserMessage'
import { checkAndRecordHomeVisit } from '@/composables/useHomeVisitMemory'
import { deferIdle } from '@/composables/useDefer'
import WelcomeBackCard from '@/components/home/WelcomeBackCard.vue'
import { campNotes } from '@/config/campNotes'
import HomeTicketCard from '@/components/home/HomeTicketCard.vue'
import HomeContinueWatching from '@/components/home/HomeContinueWatching.vue'
import HomeHero from '@/components/home/HomeHero.vue'
import HomeBanner from '@/components/home/HomeBanner.vue'
import HomeTodayReleases, { type RecentCard } from '@/components/home/HomeTodayReleases.vue'
import HomeNowPlaying from '@/components/home/HomeNowPlaying.vue'
import HomeTodoList from '@/components/home/HomeTodoList.vue'
import HomeGuideList from '@/components/home/HomeGuideList.vue'
import StopSessionDialog from '@/components/home/StopSessionDialog.vue'
import { homeApi, type HomeSummary } from '@/api/economy'
import { fetchCalendar, type CalendarItem } from '@/api/calendar'
import { ensureEmbyBase, imageUrl } from '@/api/emby'
import { groupRecentResources, stripEpisodeSuffix, type RecentResource } from '@/utils/recentResources'
import { rexDeepLink } from '@/utils/rexDeepLink'
import {
  Crown, MessageSquareDashed,
  Sparkles, TriangleAlert, Zap,
  Clapperboard, Ticket,
  RotateCcw,
} from 'lucide-vue-next'

const userStore = useUserStore()
const toast = useToast()

const loading = ref(true)
const subscriptions = ref<MySubscription[]>([])

/** 说明条幅（v2.42.2）：置顶公告优先，其余按运营位兜底；内容型条幅可关闭 */
const banners = ref<Announcement[]>([])
const dismissedBannerIds = ref<number[]>([])

// 经济速览（账号速览条数据）
const quickStats = ref({
  balance: null as number | null,
  streak: null as number | null,
  checkedToday: false,
  invited: null as number | null,
  // 签到功能开关（管理端可配）：关闭时首页隐藏签到相关文案
  checkinEnabled: true,
})

// ===== 我的面板（v2.34.0）：观影数据 / 进行中的事项 / 正在播放 =====
// 「正在播放」在这里只是**状态**（有会话才渲染），不是管理清单——完整会话列表在个人中心。
const stats = ref<WatchStats | null>(null)
const seekCounts = ref<{ active: number; completed: number } | null>(null)
const ticketCounts = ref<{ active: number; settled: number } | null>(null)
const sessions = ref<MyPlaybackSession[]>([])
const stoppingSession = ref('')

const greeting = computed(() => {
  const hour = new Date().getHours()
  if (hour < 6) return '夜深了，营地篝火还亮着'
  if (hour < 12) return '早安，营地新的一天开始了'
  if (hour < 14) return '中午好，营地开饭了'
  if (hour < 18) return '下午好，营地阳光正好'
  return '晚上好，营地灯火正暖'
})

const user = computed(() => userStore.user)

const activeSub = computed(
  () => subscriptions.value.find(s => s.status === 'active' && s.days_left > 0) || null,
)

// 会员状态卡：订阅中显示套餐与剩余天数；未订阅时展示付费墙引导
const isMember = computed(() => !!activeSub.value)
// 临期提醒：与后台「到期前 7 天提醒」同一口径（见 backend/reminders.py），
// 让收到站内信的同一刻在首屏也能看到，续费入口就在旁边
const expiringSoon = computed(() => isExpiringSoon(activeSub.value))
const gateOn = computed(() => !!userStore.user?.subscription_required)
const gateMessage = computed(() =>
  gateOn.value && !isMember.value
    ? '当前账号没有生效中的订阅，开通后即可播放全库内容'
    : '',
)
// 公益服（v2.7.0）：这个服免费开放，不需要会员，首屏不再推销会员
const isFreeRealm = computed(() => userStore.isFreeRealm)
// 套餐周期已过多少：按订阅自己的 start_date → end_date 算，不再用「剩余天数猜一个分母」——
// 那条旧公式（days_left / (days_left + 30)）画出来的进度与真实周期无关，
// 一个刚买的 30 天套餐会显示成 50%，反而让人以为已经消耗了一半。
const memberProgress = computed(() => {
  const sub = activeSub.value
  if (!sub) return 0
  const start = new Date(sub.start_date).getTime()
  const end = new Date(sub.end_date).getTime()
  if (!start || !end || end <= start) return 0   // 日期不全就不画进度
  const total = end - start
  const used = Math.min(Math.max(Date.now() - start, 0), total)
  return Math.max(1, Math.min(100, Math.round((used / total) * 100)))
})

// 票根卡的三格（暗房影院改版起由原三张资产卡合并而来）：积分 / 订阅 / 观影数据。
// 暗房影院只有一支强调色，tone 字段保留但不再决定颜色（数字一律正文色）。
const assetCards = computed(() => {
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
        desc: `${activeSub.value.plan_name} · ${activeSub.value.end_date?.slice(0, 10) || ''}到期`,
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
      // 功能色定稿（v2.42.4）：积分=金（钱的语义）、订阅=青（品牌主色）、
      // 观影数据=紫；顶栏 pill 已删（v2.42.8），资产卡是唯一的常驻资产入口
      tone: 'amber',
      title: '积分',
      value: quickStats.value.balance !== null ? quickStats.value.balance.toLocaleString() : '—',
      unit: '',
      progress: null,
      desc: quickStats.value.checkinEnabled ? '签到、邀请与兑换都能攒积分' : '邀请与兑换都能攒积分',
      // 连击天数并进说明行，数据不丢（原速览条的独立「每日签到」格不再重复）
      // 签到开关关闭时不显示签到相关文案
      note: !quickStats.value.checkinEnabled ? ''
        : quickStats.value.checkedToday
        ? (quickStats.value.streak ? `今日已签 · 连续 ${quickStats.value.streak} 天` : '今日已签 · 明天再来')
        : '今天还没签到',
      // 窄屏票面只留一行 ≤6 字的状态（长说明在宽屏才显示）
      short: !quickStats.value.checkinEnabled ? ''
        : quickStats.value.checkedToday
        ? (quickStats.value.streak ? `连签 ${quickStats.value.streak} 天` : '今日已签')
        : '今日未签',
      footer: '去钱包',
      badge: !quickStats.value.checkinEnabled ? null : (quickStats.value.checkedToday ? null : '今日未签'),
      // 未签时徽章走警示色（hot），与订阅临期同一套提醒语言
      hot: quickStats.value.checkinEnabled && !quickStats.value.checkedToday,
    },
    {
      key: 'member',
      to: '/store',
      icon: Crown,
      tone: 'cyan',
      title: '订阅',
      ...memberCard,
    },
    {
      key: 'watch',
      to: '',
      icon: Clapperboard,
      tone: 'violet',
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

// 进行中的事项：只列「自己提交的东西处理到哪了」。数量为 0 时右侧只留箭头（不写 0 也不写孤零零的「—」，
// 后者读起来像「数值坏了」），
// sub 再说清下一步会发生什么——一个孤零零的 0 读起来像「功能坏了」，不像「没事可做」。
const todoRows = computed(() => {
  const seek = seekCounts.value
  const seekActive = seek?.active ?? 0
  const ticket = ticketCounts.value
  const ticketActive = ticket?.active ?? 0
  return [
    {
      key: 'seek',
      to: '/request',
      icon: MessageSquareDashed,
      label: '我的求片',
      value: seekActive > 0 ? String(seekActive) : '',
      sub: !seek
        ? '查看求片进度'
        : seekActive > 0
          ? '入库后会在消息中心通知你'
          : seek.completed > 0
            ? `已入库 ${seek.completed} 部`
            : '还没提交过求片',
      hot: seekActive > 0,
    },
    {
      key: 'ticket',
      to: '/tickets',
      icon: Ticket,
      label: '我的工单',
      value: ticketActive > 0 ? String(ticketActive) : '',
      sub: !ticket
        ? '查看工单状态'
        : ticketActive > 0
          ? '客服回复会推送到消息中心'
          : ticket.settled > 0
            ? `已处理 ${ticket.settled} 个`
            : '遇到问题可以提交工单',
      hot: ticketActive > 0,
    },
  ]
})

async function stopSession(session: MyPlaybackSession) {
  stoppingSession.value = session.session_key
  try {
    await embyApi.stopSession(session.session_key)
    toast.success(`已结束「${session.device || '未知设备'}」上的播放`)
    sessions.value = sessions.value.filter((s) => s.session_key !== session.session_key)
  } catch (err: any) {
    toast.error(toUserMessage(err, 'stopFailed'))
  } finally {
    stoppingSession.value = ''
  }
}

// 「结束播放」是踢掉其他设备的高危操作，先弹窗二次确认
const stopTarget = ref<MyPlaybackSession | null>(null)
const showStopConfirm = computed({
  get: () => stopTarget.value !== null,
  set: (v: boolean) => { if (!v) stopTarget.value = null },
})

function askStopSession(session: MyPlaybackSession) {
  stopTarget.value = session
}

function confirmStopSession() {
  const session = stopTarget.value
  stopTarget.value = null
  if (session) stopSession(session)
}

// 会员订阅是首页核心数据（会员卡/三步指引都依赖它）：失败时显示错误态 + 重试，
// 而不是静默吞掉让用户看到错误的「未开通」形态
const loadError = ref(false)

/**
 * 首屏数据快照（v2.42.9）
 *
 * 这批数据（积分 / 连签 / 观影统计 / 求片与工单数量…）变化以天为单位，秒级刷新
 * 没有意义，但每次进页面都要等接口回来才出内容，首屏就得先闪一段骨架。
 *
 * 做法：数据到手后按账号存一份快照；下次进页面先用快照渲染真实内容，同时
 * 静默拉一次更新数字。回访用户首屏 0 骨架，只有首次访问才看得到加载态。
 *
 * 快照按 user_id 分键：同一台设备换账号登录不会看到上一个账号的数据。
 */
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
    // 隐私模式 / 配额满：快照只是优化，拿不到就算了，不影响正常加载
  }
}

/** 把聚合响应套用到各视图状态（快照与实时数据走同一条路，保证渲染口径一致） */
function applySummary(data: HomeSummary) {
  if (data.points) quickStats.value.balance = data.points.balance
  if (data.checkin) {
    quickStats.value.streak = data.checkin.streak
    quickStats.value.checkedToday = data.checkin.checked_today
    quickStats.value.checkinEnabled = data.checkin.enabled !== false
  }
  if (data.invite) quickStats.value.invited = data.invite.invited_count
  if (data.subscriptions) subscriptions.value = data.subscriptions
  if (data.announcements) {
    banners.value = data.announcements
      .filter((a) => a.is_pinned && !dismissedBannerIds.value.includes(a.id))
      .slice(0, 1)
  }
  if (data.stats) stats.value = data.stats
  if (data.media_seek) {
    const rows = data.media_seek.requests || []
    seekCounts.value = {
      active: rows.filter((r) => r.status === 'pending' || r.status === 'approved').length,
      completed: rows.filter((r) => r.status === 'completed').length,
    }
  }
  if (data.tickets) {
    ticketCounts.value = {
      active: data.tickets.filter((t) => t.status === 'open' || t.status === 'pending').length,
      settled: data.tickets.filter((t) => t.status === 'closed' || t.status === 'resolved').length,
    }
  }
  if (data.sessions) sessions.value = data.sessions.sessions || []
}

// silent = true 时为 KeepAlive 切回 tab 的后台静默刷新：不碰 loading，不闪骨架屏
const hasLoaded = ref(false)
async function loadDeferred(silent = false) {
  // 有快照：先用它出内容（不闪骨架），再静默刷新；没有才走完整加载态
  const userId = userStore.user?.id
  const snapshot = userId != null ? readSnapshot(userId) : null
  if (snapshot) {
    applySummary(snapshot)
    hasLoaded.value = true
    loading.value = false
  }
  if (!silent && !snapshot) loading.value = true
  let memberFailed = false
  try {
    // 9 项首屏数据一次取回（原来是 9 个并发请求，浏览器 6 连接上限要分两波排队）
    const data = await homeApi.summary()
    memberFailed = data.subscriptions === null
    applySummary(data)
    // 静默刷新失败时不降级：不覆盖已有数据、不弹错误态（首屏失败才走错误态）
    if (!silent || !memberFailed) {
      subscriptions.value = Array.isArray(data.subscriptions) ? data.subscriptions : []
      loadError.value = memberFailed
    }
    if (userId != null) writeSnapshot(userId, data)
    loadError.value = memberFailed
  } catch (err: any) {
    // 兜底：正常情况下到不了这里
    // 静默刷新失败，或已有快照可显示时，都不打扰用户
    if (silent || snapshot) return
    loadError.value = true
    if (err?.response?.status !== 401) {
      toast.error(toastSystem.networkError)
    }
  } finally {
    loading.value = false
    hasLoaded.value = true
  }
}

/** 错误态重试：重新拉同一批数据 */
async function retryLoad() {
  await loadDeferred()
}

function dismissBanner() {
  const current = banners.value[0]
  if (current) dismissedBannerIds.value = [...dismissedBannerIds.value, current.id]
  banners.value = banners.value.slice(1)
}

/** 运营位条幅：无公告时按账号状态兑底（公益服 / 未开通），都是静态文案不需请求 */
const fallbackBanner = computed(() => {
  if (isFreeRealm.value) {
    return { icon: Sparkles, tone: 'cyan' as const, title: '公益服 · 免费开放', text: realmNoteText() }
  }
  if (!isMember.value) {
    return { icon: Crown, tone: 'amber' as const, title: '会员未开通', text: '开通后即可播放全库内容，票根上的「立即开通」可直接前往。' }
  }
  return null
})

function realmNoteText() {
  return userStore.realmNote || '本服为公益服 · 免费开放：无需开通会员即可观看全库内容。'
}


// ===== 暗房影院 暗房影院：幕布 Hero / 今日入库（追新日历近 7 天） =====
// 只读一次追新日历：同一份数据既给 Hero 当背景图，也给「今日入库」海报横滑。
// 失败（未开通被拦、后端老版本没有这个端点）一律静默：Hero 回落到暖黑渐变，海报行不渲染。
const recentItems = ref<CalendarItem[]>([])
const recentHasToday = ref(false)

function isoDay(d: Date): string {
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${d.getFullYear()}-${m}-${day}`
}

async function loadRecent() {
  const end = new Date()
  const start = new Date(end.getFullYear(), end.getMonth(), end.getDate() - 6)
  try {
    // 图片地址要拼在 EA 地址上：和日历并行拉一次账号卡，免得刚登录时拼出同源 /emby/... 全部 404
    const [res] = await Promise.all([
      fetchCalendar({ start: isoDay(start), end: isoDay(end) }),
      ensureEmbyBase(),
    ])
    const days = [...res.days].sort((a, b) => (a.date < b.date ? 1 : -1))
    recentHasToday.value = days.some((d) => d.date === isoDay(end) && d.items.length > 0)
    // 按「新 → 旧」摊平：天倒序，天内后端已按入库时间倒序；归并成资源在 recentCards 里做
    recentItems.value = days.flatMap((d) => d.items)
    posterAttempts.value = {}
  } catch {
    recentItems.value = []
  }
}

/** Hero 背景：近 7 天入库里第一张带背景图（Backdrop）的条目——眉题写的就是这一条 */
const heroItem = computed(() => recentItems.value.find((i) => i.BackdropImageTags?.length) || null)
// 缩略图串图已在服务端修复（见 api/emby.ts imageUrl 的说明），背景图按 1280 宽取
const heroImage = computed(() => (heroItem.value ? imageUrl(heroItem.value.Id, 'Backdrop', 1280) : ''))
const heroImageFailed = ref(false)

function recentTitle(item: CalendarItem): string {
  if (item.Type === 'Episode') return item.SeriesName || stripEpisodeSuffix(item.Name)
  return item.Name
}

/**
 * 海报行的卡片：**一部剧 / 一部电影一张**，不出单集（归并规则见 utils/recentResources）。
 *
 * - 片名只写剧名 / 电影名，不带「第 N 集」/ SxxExx；
 * - 图片按候选顺序试：单集 → 剧的 Primary → 单集 Primary（服务端按 集→季→剧 回退）→ Thumb；
 *   电影 / 剧 → 自己的 Primary → Thumb。某一张加载失败就换下一张，全失败才落排版占位卡；
 * - 海报行卡片宽 104–120px，取 maxWidth=320（覆盖 2.5x 屏）。
 */
const POSTER_WIDTH = 320

/** 每张卡已经失败了几张候选图（key → 已失败数） */
const posterAttempts = ref<Record<string, number>>({})

function cardHref(r: RecentResource): string {
  // 单集归并成剧之后按剧名搜；电影 / 剧本体有 TMDB id 时精确跳
  return rexDeepLink({
    Type: r.type,
    Name: r.title,
    ProviderIds: r.tmdbId ? { Tmdb: r.tmdbId } : null,
  })
}

const recentResources = computed(() => groupRecentResources(recentItems.value, 12))

const recentCards = computed<RecentCard[]>(() =>
  recentResources.value.map((r) => {
    const candidate = r.posters[posterAttempts.value[r.key] || 0]
    // 同一部剧多集合并成一张卡时，标题显示"更新至 N 集"
    const title =
      r.type === 'Series' && r.episodeCount > 1 && r.maxEpisode != null
        ? `${r.title}（更新至 ${r.maxEpisode} 集）`
        : r.title
    return {
      key: r.key,
      title,
      year: r.year,
      poster: candidate ? imageUrl(candidate.itemId, candidate.kind, POSTER_WIDTH) : '',
      href: cardHref(r),
    }
  }),
)

function onPosterError(card: RecentCard) {
  posterAttempts.value = { ...posterAttempts.value, [card.key]: (posterAttempts.value[card.key] || 0) + 1 }
}

// ===== 继续观看：最多 3 条，最近播放的在前（口径见文件头） =====
const RESUME_MAX = 3
// 卡片左侧剧照约占 40% 宽（手机 ~150px，桌面 ~220px），取 480 覆盖 2x～3x 屏
const RESUME_THUMB_WIDTH = 480

const resumeItems = ref<PortalResumeItem[]>([])
let resumeLoaded = false
/** 每张卡已经失败了几张候选图（key → 已失败数） */
const resumeThumbAttempts = ref<Record<string, number>>({})

async function loadResume() {
  try {
    // 剧照地址拼在 EA 地址上：与列表并行等账号卡就绪，刚登录时才不会拼出同源 /emby/... 全部 404
    const [res] = await Promise.all([embyApi.getResume(RESUME_MAX * 2), ensureEmbyBase()])
    resumeItems.value = (res?.items || []).slice(0, RESUME_MAX)
    resumeThumbAttempts.value = {}
  } catch {
    // 未开通 / 网络错误：整段不渲染，不打扰首屏；切回来时还会再试
    if (!resumeLoaded) resumeItems.value = []
  } finally {
    resumeLoaded = true
  }
}

interface ResumeCard {
  key: string
  /** 《剧名》 第 N 集 / 《片名》 */
  title: string
  /** 排版占位卡上的片名（不带书名号与集号） */
  name: string
  /** 客户端 · 设备；都没有就是空串（整行不渲染） */
  meta: string
  /** 0–100；没有时长时为 null（不画进度条，不猜百分比） */
  percent: number | null
  /** 当前要试的剧照地址；空串 = 候选已试完，走排版占位卡 */
  thumb: string
  href: string
}

/**
 * 剧照候选（16:9）：单集 → 单集 Thumb → 剧 Thumb → 剧 Primary；电影 → Thumb → Primary。
 * Thumb 服务端按横版背景图（Backdrop 链）出图；Primary 是竖版海报，object-fit 裁成横版兜底。
 */
function resumeThumbs(r: PortalResumeItem): { itemId: string; kind: 'Thumb' | 'Primary' }[] {
  const list: { itemId: string; kind: 'Thumb' | 'Primary' }[] = []
  if (r.episode_id) list.push({ itemId: r.episode_id, kind: 'Thumb' })
  list.push({ itemId: r.id, kind: 'Thumb' })
  if (r.poster_url) list.push({ itemId: r.id, kind: 'Primary' })
  return list
}

const resumeCards = computed<ResumeCard[]>(() =>
  resumeItems.value.map((r) => {
    const key = r.episode_id || r.id
    const isSeries = r.type === 'series'
    // 剧名优先用后端明确给的 series_name，兜底用 name；防止只显示集数看不到是哪部剧
    const seriesName = (r.series_name || r.name || '').trim()
    const ep = isSeries && r.episode_number != null ? `第 ${r.episode_number} 集` : ''
    const title = isSeries && ep ? `《${seriesName}》${ep}` : `《${seriesName || r.name}》`
    const candidate = resumeThumbs(r)[resumeThumbAttempts.value[key] || 0]
    const duration = r.duration_ticks || 0
    return {
      key,
      title,
      name: seriesName || r.name,
      meta: [r.client, r.device].filter((v): v is string => !!v).join(' · '),
      percent: duration > 0 ? Math.max(0, Math.min(100, Math.round(r.progress || 0))) : null,
      thumb: candidate ? imageUrl(candidate.itemId, candidate.kind, RESUME_THUMB_WIDTH) : '',
      // 与「本周入库」同一条跳转规则：电影 / 剧有 TMDB id 精确跳，否则按名字搜
      href: rexDeepLink({
        Type: isSeries ? 'Series' : 'Movie',
        Name: seriesName || r.name,
        ProviderIds: r.tmdb_id ? { Tmdb: r.tmdb_id } : null,
      }),
    }
  }),
)

function onResumeThumbError(card: ResumeCard) {
  resumeThumbAttempts.value = {
    ...resumeThumbAttempts.value,
    [card.key]: (resumeThumbAttempts.value[card.key] || 0) + 1,
  }
}

// ===== 「好久不见」欢迎卡（装修第二期 U2）：>72h 未进首页才显示 =====
// 访问记忆按账号隔离（localStorage 只存时间戳）；先读后写，不阻塞首屏、不新增请求。
const accountId = computed(() => userStore.user?.id ?? null)
const showWelcomeBack = ref(false)

/** 欢迎卡文案：按继续观看第一条真实数据生成（resumeCards.title 已是"《剧名》第 N 集"口径），无则通用文案 */
const welcomeCard = computed(() => {
  const first = resumeCards.value[0]
  if (!first) {
    return { message: '欢迎回来，找一部想看的作品吧', href: '' }
  }
  return { message: `上次看到${first.title}，接着看吧`, href: first.href }
})

watch(
  accountId,
  (id, oldId) => {
    // 切号：清掉旧账号的欢迎态与继续观看，防止串号闪现
    if (oldId != null && id !== oldId) {
      showWelcomeBack.value = false
      resumeItems.value = []
      resumeThumbAttempts.value = {}
      resumeLoaded = false
    }
    showWelcomeBack.value = id != null ? checkAndRecordHomeVisit(id) : false
  },
  { immediate: true },
)

/** Hero 眉题：有片单时只写「今晚放映 · 今日新片」——片名是 H1 的主角，眉题不再重复 */
const heroEyebrow = computed(() => {
  if (!heroItem.value) return '今晚放映'
  return `今晚放映 · ${recentHasToday.value ? '今日新片' : '本周新片'}`
})

/** 问候语拆成 lead + tail：名字插在中间才顺——"夜深了，影迷，今晚看什么？"
 *  （直接拼 "夜深了，来部电影？，影迷" 会冒出 "?，" 这种怪标点）
 *  营地味时段语：全站"探险营地"世界观在首页的落点 */
const greetingParts = computed(() => {
  const hour = new Date().getHours()
  if (hour < 6) return { lead: '夜深了，营地篝火还亮着', tail: '' }
  if (hour < 12) return { lead: '早安', tail: '，营地新的一天开始了' }
  if (hour < 14) return { lead: '中午好', tail: '，营地开饭了' }
  if (hour < 18) return { lead: '下午好', tail: '，营地阳光正好' }
  return { lead: '晚上好', tail: '，营地灯火正暖' }
})

/** 营地晨间便签：按日期种子每天随机取一条，零请求、纯本地 */
const campNote = computed(() => {
  const d = new Date()
  const seed = d.getFullYear() * 10000 + (d.getMonth() + 1) * 100 + d.getDate()
  return campNotes[seed % campNotes.length]
})

/** Hero 主角：和 heroImage 同一条片子（近 7 天入库里第一条带背景图的）——
 *  银幕上只讲一部片，这是影院和货架的区别 */
const heroFeatureTitle = computed(() => (heroItem.value ? recentTitle(heroItem.value) : ''))

/** 主角元信息：年份 · 电影/剧集（一行小字，不抢片名的风头） */
const heroMeta = computed(() => {
  const it = heroItem.value
  if (!it) return ''
  const parts: string[] = []
  if (it.ProductionYear) parts.push(String(it.ProductionYear))
  parts.push(it.Type === 'Movie' ? '电影' : '剧集')
  return parts.join(' · ')
})

/** 主角跳转：和海报行同一条 Rex deep link 规则（单集按剧名搜，不填单集 TMDB id，见 rexDeepLink） */
const heroHref = computed(() => {
  const it = heroItem.value
  if (!it) return ''
  return rexDeepLink({ Name: it.Name, Type: it.Type, SeriesName: it.SeriesName, ProviderIds: it.ProviderIds ?? null })
})

/** Hero 状态行：会员状态 + 签到（都是首页已有的数据，不额外请求）
 *  签到开关关闭时不显示签到状态 */
const heroStatus = computed(() => {
  const parts: string[] = []
  if (isMember.value && activeSub.value) parts.push(`会员剩 ${activeSub.value.days_left} 天`)
  else if (isFreeRealm.value) parts.push('公益服 · 免费开放')
  else parts.push('会员未开通')
  if (sessions.value.length) parts.push(`${sessions.value.length} 台设备正在播放`)
  else if (quickStats.value.checkinEnabled) parts.push(quickStats.value.checkedToday ? '今日已签到' : '今日还没签到')
  return parts.join(' · ')
})

// 票根：订阅那一格的 CTA 文案就是票根上的按钮（续费 / 立即开通 / 查看套餐）
const memberStat = computed(() => assetCards.value.find((c) => c.key === 'member') || null)

// 首屏只出骨架：数据统一走 loadDeferred（公告与资产同批，不再分关键/延后两波）
// 追新日历与继续观看非首屏关键路径：延迟到浏览器空闲后再拉
onMounted(() => {
  loadDeferred()
  deferIdle(() => { void loadRecent() })
  deferIdle(() => { void loadResume() })
})

// 从别的 tab 切回来（KeepAlive 缓存命中）：后台静默刷新，不闪骨架屏
onActivated(() => {
  if (hasLoaded.value) void loadDeferred(true)
  // 继续观看：刚在客户端看完切回来，进度要对得上（首次挂载由 onMounted 拉，这里不重复）
  if (resumeLoaded) void loadResume()
})
</script>

<template>
  <div class="home-view page">
    <HomeHero
      :hero-image="heroImage"
      :hero-image-failed="heroImageFailed"
      :has-loaded="hasLoaded"
      :hero-eyebrow="heroEyebrow"
      :hero-feature-title="heroFeatureTitle"
      :hero-meta="heroMeta"
      :hero-status="heroStatus"
      :greeting="greeting"
      :greeting-parts="greetingParts"
      :username="user?.username"
      :is-member="isMember"
      :is-free-realm="isFreeRealm"
      :hero-href="heroHref"
      @image-error="heroImageFailed = true"
    />

    <!-- 营地晨间便签：Hero 下方一张小纸片条，每天一句营地文案（零请求） -->
    <div class="camp-note" role="note" aria-label="营地晨间便签">
      <span class="camp-note-pin" aria-hidden="true">📌</span>
      <span class="camp-note-text">{{ campNote }}</span>
    </div>

    <main class="container main">
      <!-- 会员数据加载失败：给错误态 + 重试，而不是静默显示「未开通」 -->
      <div v-if="loadError && !loading" class="au-empty load-error-card au-card">
        <TriangleAlert :size="28" />
        <p>会员信息加载失败，页面数据可能不完整</p>
        <button class="au-btn au-btn-primary au-btn-sm" @click="retryLoad">
          <RotateCcw :size="13" />
          重新加载
        </button>
      </div>

      <HomeBanner
        :loading="loading"
        :banners="banners"
        :fallback-banner="fallbackBanner"
        @close="dismissBanner"
      />

      <HomeTicketCard
        :loading="loading"
        :asset-cards="assetCards"
        :member-footer="memberStat?.footer || '查看套餐'"
        :plan-name="activeSub?.plan_name || ''"
      />

      <HomeTodayReleases
        :items="recentCards"
        :has-today="recentHasToday"
        @poster-error="onPosterError"
      />

      <!-- 「好久不见」欢迎卡：>72h 未进首页才显示；纯展示，不阻塞首屏、不新增请求 -->
      <WelcomeBackCard
        v-if="showWelcomeBack"
        :message="welcomeCard.message"
        :href="welcomeCard.href"
      />

      <HomeContinueWatching
        :cards="resumeCards"
        @thumb-error="onResumeThumbError"
      />

      <HomeNowPlaying
        :sessions="sessions"
        :stopping-session="stoppingSession"
        @stop-session="askStopSession"
      />

      <HomeTodoList :loading="loading" :rows="todoRows" />

      <HomeGuideList />

      <StopSessionDialog
        :visible="showStopConfirm"
        :device-name="stopTarget?.device || ''"
        @confirm="confirmStopSession"
        @cancel="showStopConfirm = false"
      />
    </main>
  </div>
</template>


<style scoped>
.home-view {
  min-height: 100dvh;
  color: var(--au-text);
}

/* 营地晨间便签：v4 纸片风小纸条，Hero 下方一条 */
.camp-note {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 14px auto 0;
  max-width: 560px;
  width: calc(100% - 40px);
  padding: 10px 16px;
  background: #fffdf6;
  border: 1.5px dashed #d8cdb8;
  border-radius: 4px 14px 4px 14px;
  box-shadow: 1px 2px 0 rgba(0, 0, 0, 0.06);
  transform: rotate(-0.4deg);
}
.camp-note-pin {
  font-size: 18px;
  line-height: 1;
  flex-shrink: 0;
}
.camp-note-text {
  font-size: 13px;
  font-weight: 700;
  color: #2a2620;
  line-height: 1.6;
}

.main {
  padding-top: 1.5rem;
  padding-bottom: 4rem;
  display: flex;
  flex-direction: column;
}

.load-error-card {
  margin-bottom: 1.25rem;
}

/* ==================== 响应式 ==================== */

@media (--au-bp-md) {
  .main {
    /* 底部安全区改由全局 .page 统一消费（--au-dock-space，见 aurora.css），这里只留页面基数 3rem */
    padding-bottom: 3rem;
  }
}

@media (--au-bp-sm) {
  .main {
    padding-left: var(--gutter);
    padding-right: var(--gutter);
  }
}
</style>


