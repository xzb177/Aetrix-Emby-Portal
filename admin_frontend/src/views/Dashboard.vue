<script setup lang="ts">
/**
 * 仪表盘 — 经营驾驶舱
 *
 * v2.25.0：顶部换成**交付链数据卡**（用户总数 / 当前播放 / 待处理求片 / 待处理工单 /
 * 扫描状态 / 存储健康）：先看「现在能不能用、有没有要处理的」，再往下才是经营与排行。
 * 每张卡都直接点进对应页面，不用先想「这个数字在哪一页」。
 *
 * v2.42.10（Phase 5·KPI 深链）：**每一个数字都能点**，而且点进去**已经带上筛选**——
 * 「待处理工单 3」进的是 `/tickets?status=open`（只看待处理的那几条），不是全部工单；
 * 「待支付订单」进 `/orders?status=pending`。落地页用 `useQueryFilter` 接这个参数
 * （见 composables/useQueryFilter.ts），所以从仪表盘、待办条、命令面板点过去口径一致。
 *
 * - 顶部「待办」条：待处理工单 / 待审求片 / 待支付订单，一点直达对应页面（带筛选）
 * - 交易概览：今日营收、累计营收、积分存量、今日签到、兑换码核销、邀请人数（均可点）
 * - 趋势图：近 7/14/30 天的新增用户 / 播放 / 营收 / 签到（纯 SVG，无额外依赖）
 * - 播放榜：用户榜 + 热门内容榜
 *
 * v2.54（暗房影院）：整页改用 components/ui 的共享原语（PageHeader / SectionCard /
 * StatTile / EmptyState），是其它页面改造的参考实现——见 components/ui/README.md。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import {
  Film, MessageSquareDashed, Ticket, Users, Wallet,
  Coins, CalendarCheck, TicketCheck, Gift, ArrowRight, TrendingUp,
  Server, HardDrive, ScanSearch, CloudDownload, Download, Route as RealmIcon, Trophy, Flame, Activity,
  AlertTriangle,
} from 'lucide-vue-next'
import { EmptyState, PageHeader, SectionCard, StatTile } from '@/components/ui'
import {
  fetchLibraries, fetchOverview, fetchPlaybackStats, fetchRealmOverview,
  fetchServersSummary, fetchStatsTrend, fetchBackendServices, fetchDashboardAggregate,
  type BackendServiceStatus, type DashboardAggregate } from '@/api/admin'
import { fetchEconomyStats, type EconomyStats } from '@/api/economy'
import type {
  EmbyLibrary, OverviewStats, PlaybackStats, RealmOverview, ServerSummary, TrendStats,
} from '@/types'

const overview = ref<OverviewStats | null>(null)
const playback = ref<PlaybackStats | null>(null)
const economy = ref<EconomyStats | null>(null)
const trend = ref<TrendStats | null>(null)
// 存储健康卡按库的本机目录可读性判（见 mountSummary）。以前这里还单独拉一次 /mounts，
// 结果从没被用到——「存储来源」独立页撤销后成了每次进仪表盘白打的一个请求，已去掉。
const libraries = ref<EmbyLibrary[]>([])
/** 服务器接入情况：面板到底接了几台后端服 / 几台 Emby 服 / 有没有接下载器 */
const servers = ref<ServerSummary | null>(null)
/** 后端服务：aetrix-api + aetrix-worker 的运行状态 */
const backendServices = ref<BackendServiceStatus[]>([])
/** 多服运营：每个服的会员 / 内容 / 节点，一个面板同时管几个服一眼看完 */
const realms = ref<RealmOverview | null>(null)
const loading = ref(true)
const trendLoading = ref(false)
/** 核心三项（概览 / 播放 / 经营）里拿不到的那几项：页面照常渲染其余部分，顶部给一条可重试的提示 */
const failedParts = ref<string[]>([])

const days = ref(14)
type Metric = 'new_users' | 'plays' | 'revenue' | 'checkins'
const metric = ref<Metric>('new_users')
// 趋势图指标色（暗房影院）：走 --au-* 令牌，随深浅主题与品牌色切换。
// SVG 的 stroke / stop-color **属性**不认 var()，所以模板里一律写进 style（CSS 属性认）。
const metricTabs: { key: Metric; label: string; color: string }[] = [
  { key: 'new_users', label: '新增用户', color: 'var(--au-primary)' },
  { key: 'plays', label: '播放次数', color: 'var(--au-info)' },
  { key: 'revenue', label: '营收 (¥)', color: 'var(--au-success)' },
  { key: 'checkins', label: '签到次数', color: 'var(--au-danger)' },
]

async function loadTrend() {
  trendLoading.value = true
  try {
    trend.value = await fetchStatsTrend(days.value)
  } catch {
    // GET 静默：趋势拿不到就显示空图，不让切 7/14/30 天抛出未处理的 rejection
    trend.value = null
  } finally {
    trendLoading.value = false
  }
}

/**
 * 聚合优先：一次拿回首屏 8 路数据（省 7 次跨洋往返）。
 * 聚合接口本身失败（网络/5xx/鉴权等）时返回 false，调用方回退逐个调用。
 * parts 里为 null 的项按原 soft() 语义点名并给重试。
 */
async function loadAggregate(): Promise<boolean> {
  let agg: DashboardAggregate
  try {
    agg = await fetchDashboardAggregate(days.value)
  } catch {
    return false
  }
  const parts = agg?.parts
  if (!parts) return false
  const failed: string[] = []
  const take = <T,>(label: string, v: T | null): T | null => {
    if (v == null) failed.push(label)
    return v
  }
  overview.value = take('概览', parts.overview)
  playback.value = take('播放统计', parts.playback)
  economy.value = take('经营数据', parts.economy)
  trend.value = parts.trend
  libraries.value = parts.libraries?.libraries ?? []
  servers.value = parts.servers ?? null
  realms.value = parts.realms ?? null
  backendServices.value = parts.backend_services?.services ?? []
  failedParts.value = failed
  loading.value = false
  return true
}

/**
 * 首屏：优先走聚合接口，失败则回退原来的 8 路并行逐个调用。
 * （聚合只是把"逐个调用"搬到服务端一次完成，任一形态下各区块的
 *  失败兜底语义保持一致：失败的那几项在顶部点名并给「重试」。）
 */
async function loadAll() {
  loading.value = true
  if (await loadAggregate()) return  // 内部已收尾 loading/failedParts
  const failed: string[] = []
  const soft = <T,>(label: string, p: Promise<T>) => p.catch(() => { failed.push(label); return null })
  try {
    const [o, p, e, libraryData, serverData, realmData, backendData] = await Promise.all([
      soft('概览', fetchOverview()),
      soft('播放统计', fetchPlaybackStats()),
      soft('经营数据', fetchEconomyStats()),
      fetchLibraries().catch(() => ({ libraries: [] as EmbyLibrary[] })),
      fetchServersSummary().catch(() => null),
      fetchRealmOverview().catch(() => null),
      fetchBackendServices().catch(() => ({ services: [] as BackendServiceStatus[] })),
      loadTrend(),
    ])
    overview.value = o
    playback.value = p
    economy.value = e
    libraries.value = libraryData.libraries
    servers.value = serverData
    realms.value = realmData
    backendServices.value = (backendData as { services?: BackendServiceStatus[] } | null)?.services || []
  } finally {
    failedParts.value = failed
    loading.value = false
  }
}

onMounted(loadAll)

// ==================== 服务器接入（信息展示）====================

const SERVER_ICONS: Record<string, unknown> = {
  ea: Server,
  emby: HardDrive,
  moviepilot: CloudDownload,
  qbittorrent: Download,
}

const serverTiles = computed(() => {
  const kinds = servers.value?.kinds || {}
  return Object.values(kinds).map((stat) => ({
    stat,
    icon: SERVER_ICONS[stat.kind] || Server,
  }))
})

watch(days, loadTrend)

// ==================== 待办 ====================

const todos = computed(() => {
  const list: { label: string; count: number; to: string; icon: unknown; tone: string }[] = []
  if (overview.value) {
    list.push({ label: '待处理工单', count: overview.value.tickets.open, to: '/tickets?status=open', icon: Ticket, tone: 'danger' })
    list.push({ label: '待审求片', count: overview.value.media_seeks.pending, to: '/media-seek?status=pending', icon: MessageSquareDashed, tone: 'warning' })
  }
  if (economy.value) {
    list.push({ label: '待支付订单', count: economy.value.orders.pending, to: '/orders?status=pending', icon: Wallet, tone: 'info' })
  }
  return list
})

const todoTotal = computed(() => todos.value.reduce((s, t) => s + t.count, 0))

// ==================== 交付链数据卡 ====================


/** 扫描状态：正常 / 异常（partial、failed）/ 正在扫 / 从没扫过 */
const scanSummary = computed(() => {
  const list = libraries.value
  const statusOf = (l: EmbyLibrary) => (l.is_scanning ? 'running' : l.last_scan?.status || '')
  return {
    total: list.length,
    ok: list.filter((l) => statusOf(l) === 'success').length,
    bad: list.filter((l) => statusOf(l) === 'partial' || statusOf(l) === 'failed').length,
    scanning: list.filter((l) => statusOf(l) === 'running').length,
    never: list.filter((l) => !l.last_scan && !l.is_scanning).length,
  }
})

/**
 * 存储健康：以 EM（面板进程）能不能碰到为准（em_reachable），没有体检结果时退回落库结果
 *
 * 「被媒体库绑定、但播放节点（EA）够不着」是唯一阻断播放的一种，单独点出来。
 */
/** 存储卡改看**本机目录可读性**（v2.46.0）

之前这里看的是 EA 可达性（旧的挂载体检状态），但面板自己真正会因挂载断掉而扫不到的是
本机路径：那个路径在容器里读不到，扫描就会把它当成“来源没了”。所以改成按库的本机目录
判：ok=能列举 / false=有路径读不到 / null=纯远程或没配本机路径，不参与判定。
 */
const mountSummary = computed(() => {
  const libs = libraries.value.filter((l) => !l.is_virtual)
  const local = libs.filter((l) => l.local_dirs_readable !== null)
  const failed = local.filter((l) => l.local_dirs_readable === false)
  return {
    total: libs.length,
    ok: local.filter((l) => l.local_dirs_readable === true).length,
    failed: failed.length,
    unchecked: libs.length - local.length,
    blocked: libs.filter((l) => (l.local_dirs_readable === false)
      && (l.path_entries?.length || 0) > 0).length,
    failedNames: failed.slice(0, 2).map((l) => l.name).join('、'),
    failedMore: failed.length > 2,
  }
})

type KpiTone = 'plain' | 'ok' | 'info' | 'warn' | 'danger'

/** 顶部五张卡：一条交付链看下来（用户 → 求片 → 工单 → 内容 → 存储） */
const kpis = computed<{
  key: string; label: string; value: number | string; foot: string
  to: string; icon: unknown; tone: KpiTone; title: string
}[]>(() => {
  const scan = scanSummary.value
  const mount = mountSummary.value
  return [
    {
      key: 'users', label: '用户总数', value: overview.value?.users.total ?? 0,
      foot: `活跃 ${overview.value?.users.active ?? 0} · 今日播放 ${playback.value?.today.plays ?? 0} 次`,
      to: '/users', icon: Users, tone: 'plain', title: '用户与账号',
    },
    {
      key: 'seeks', label: '待处理求片', value: overview.value?.media_seeks.pending ?? 0,
      foot: (overview.value?.media_seeks.pending ?? 0) > 0 ? '点开只看待审核' : '没有待处理求片',
      to: '/media-seek?status=pending', icon: MessageSquareDashed,
      tone: (overview.value?.media_seeks.pending ?? 0) > 0 ? 'warn' : 'plain',
      title: '求片与内容',
    },
    {
      key: 'tickets', label: '待处理工单', value: overview.value?.tickets.open ?? 0,
      foot: (overview.value?.tickets.open ?? 0) > 0 ? '点开只看进行中' : '没有待处理工单',
      to: '/tickets?status=open', icon: Ticket,
      tone: (overview.value?.tickets.open ?? 0) > 0 ? 'danger' : 'plain',
      title: '服务支持',
    },
    {
      key: 'scan', label: '扫描状态', value: `${scan.ok} 个正常/共 ${scan.total} 个库`,
      foot: scan.total
        ? [
            scan.bad ? `异常 ${scan.bad}` : '',
            scan.scanning ? `扫描中 ${scan.scanning}` : '',
            scan.never ? `未扫过 ${scan.never}` : '',
          ].filter(Boolean).join('\n') || '全部正常'
        : '还没有媒体库',
      to: '/emby', icon: ScanSearch,
      tone: scan.bad ? 'warn' : scan.scanning ? 'info' : scan.total ? 'ok' : 'plain',
      title: '媒体库与扫描',
    },
    {
      key: 'mounts', label: '存储健康', value: `${mount.ok} 个正常/共 ${mount.total} 条来源`,
      foot: mount.total
        ? [
            mount.failed ? `异常 ${mount.failed}${mount.failedNames ? `（${mount.failedNames}${mount.failedMore ? ' 等' : ''}）` : ''}` : '',
            mount.unchecked ? `未体检 ${mount.unchecked}` : '',
            mount.blocked ? `播放节点够不着 ${mount.blocked}` : '',
          ].filter(Boolean).join('\n') || '全部正常'
        : '还没有存储来源',
      to: '/servers', icon: HardDrive,
      tone: mount.failed || mount.blocked ? 'warn' : mount.total ? 'ok' : 'plain',
      title: '存储来源（按服务器配置）',
    },
  ]
})

// ==================== 趋势图 ====================

const currentMetric = computed(() => metricTabs.find((t) => t.key === metric.value)!)

const series = computed(() => trend.value?.series ?? [])

const values = computed(() => series.value.map((p) => Number(p[metric.value]) || 0))

const maxValue = computed(() => Math.max(...values.value, 1))

const CHART_W = 720
const CHART_H = 150
const PAD = 14

const points = computed(() => {
  const list = values.value
  const n = list.length
  if (n === 0) return []
  const step = n > 1 ? CHART_W / (n - 1) : 0
  return list.map((v, i) => {
    const x = i * step
    const y = CHART_H - PAD - (v / maxValue.value) * (CHART_H - PAD * 2)
    return [x, y] as const
  })
})

const linePath = computed(() =>
  points.value.map((p, i) => `${i === 0 ? 'M' : 'L'}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(' '),
)

const areaPath = computed(() =>
  points.value.length ? `${linePath.value} L${CHART_W} ${CHART_H} L0 ${CHART_H} Z` : '',
)

/** x 轴标签：首 / 中 / 末，避免拥挤 */
const axisLabels = computed(() => {
  const list = series.value
  if (list.length === 0) return []
  const idx = [0, Math.floor(list.length / 2), list.length - 1]
  return [...new Set(idx)].map((i) => list[i].date.slice(5))
})

function fmtMoney(v: number): string {
  return `¥${v.toFixed(2)}`
}

function libraryStatus(library: EmbyLibrary): string {
  if (library.is_scanning) return '扫描中'
  if (!library.is_enabled) return '已停用'
  return '正常'
}

/** 后端服务状态中文映射：redis_unavailable 这类英文枚举不直接上界面 */
const SERVICE_STATUS_LABEL: Record<string, string> = {
  healthy: '运行中',
  degraded: '降级',
  redis_unavailable: 'Redis 不可用',
  unreachable: '心跳丢失',
  unknown: '状态未知',
  no_heartbeat: '无心跳',
  stale: '心跳过期',
  error: '读取异常',
}

function serviceStatusLabel(status: string): string {
  return SERVICE_STATUS_LABEL[status] || status
}

/** 后端服务名中文映射（aetrix-api 这类内部名不直接上界面） */
const SERVICE_NAME_LABEL: Record<string, string> = {
  'aetrix-api': 'API 服务',
  'aetrix-worker': '后台任务服务',
}

function serviceNameLabel(name: string): string {
  return SERVICE_NAME_LABEL[name] || name
}

/** 后端服务角色中文映射 */
const SERVICE_ROLE_LABEL: Record<string, string> = {
  api: '接口服务',
  worker: '后台任务',
}

function serviceRoleLabel(role: string): string {
  return SERVICE_ROLE_LABEL[role] || role
}

</script>

<template>
  <div class="admin-page dashboard">
    <PageHeader
      eyebrow="概览"
      title="经营驾驶舱"
      description="先看现在能不能用、有没有要处理的，再看经营与排行。每个数字都能点进带好筛选的明细页。"
    >
      <template #actions>
        <RouterLink to="/health" class="au-btn au-btn-ghost au-btn-sm">
          <Activity :size="14" /> 服务健康
        </RouterLink>
      </template>
    </PageHeader>

    <!-- 首屏骨架：与真实布局同形，不再是一行「加载中…」 -->
    <div v-if="loading" class="dash-skeleton" aria-busy="true" aria-label="加载中">
      <div v-for="n in 5" :key="n" class="au-skeleton sk-tile" />
      <div class="au-skeleton sk-wide" />
    </div>

    <template v-else>
      <el-alert
        v-if="failedParts.length"
        type="warning"
        :closable="false"
        show-icon
        :title="`部分数据没拿到：${failedParts.join('、')}。下面对应的数字暂按 0 显示。`"
      >
        <template #default>
          <el-button size="small" :icon="AlertTriangle" @click="loadAll">重新加载</el-button>
        </template>
      </el-alert>

      <!--
        交付链数据卡：先回答「现在能不能用、有没有要处理的」——
        用户 → 求片 → 工单 → 内容（扫描）→ 存储，每张卡点进去就是这个数的明细页。
      -->
      <section class="kpi-grid" aria-label="交付链">
        <StatTile
          v-for="k in kpis"
          :key="k.key"
          layout="icon-left"
          :label="k.label"
          :value="k.value"
          :hint="k.foot"
          :icon="k.icon"
          :tone="k.tone"
          :to="k.to"
          :title="k.title"
        />
      </section>

      <!-- 待办 -->
      <section v-if="todos.length" class="todo-bar" :class="{ clear: todoTotal === 0 }" aria-label="待办">
        <div class="todo-lead">
          <span class="todo-lead-label">{{ todoTotal === 0 ? '全部处理完毕' : '待办事项' }}</span>
          <span v-if="todoTotal > 0" class="todo-lead-count">{{ todoTotal }}</span>
        </div>
        <RouterLink
          v-for="t in todos"
          :key="t.label"
          :to="t.to"
          class="todo-pill"
          :class="[t.tone, { zero: t.count === 0 }]"
        >
          <component :is="t.icon" :size="14" />
          <span>{{ t.label }}</span>
          <strong>{{ t.count }}</strong>
          <ArrowRight :size="13" class="todo-arrow" />
        </RouterLink>
      </section>

      <!--
        服务器接入：接了什么、几台能用、当前用哪台（明细在「服务器与线路」页）。
        后端服务：aetrix-api + aetrix-worker 运行状态。两组同一行网格。
      -->
      <section v-if="serverTiles.length || backendServices.length" class="tile-grid" aria-label="服务接入">
        <StatTile
          v-for="tile in serverTiles"
          :key="tile.stat.kind"
          to="/servers"
          :icon="tile.icon"
          :label="tile.stat.short"
          :value="tile.stat.total"
          :suffix="`台 · 可用 ${tile.stat.reachable}`"
          :tone="tile.stat.reachable > 0 ? 'ok' : 'plain'"
          :hint="tile.stat.activatable
            ? (tile.stat.active_name || '未设置当前使用')
            : (tile.stat.reachable > 0 ? '可用于求片' : '未连接')"
        >
          <template v-if="tile.stat.active_name" #label-extra>
            <span class="au-badge au-badge-amber tile-badge">当前</span>
          </template>
        </StatTile>
        <StatTile
          v-for="svc in backendServices"
          :key="svc.name"
          :icon="Server"
          :label="serviceNameLabel(svc.name)"
          :value="serviceStatusLabel(svc.status)"
          :suffix="svc.lag_seconds != null ? `· 心跳 ${svc.lag_seconds}s 前` : ''"
          :tone="svc.status === 'healthy' ? 'ok' : 'warn'"
          :hint="svc.role === 'worker' && svc.pid ? `进程号 ${svc.pid}` : '对外接口服务'"
          class="svc-tile"
        >
          <template #label-extra>
            <span class="au-badge au-badge-muted tile-badge">{{ serviceRoleLabel(svc.role) }}</span>
          </template>
        </StatTile>
      </section>

      <!--
        各服概况：每个服的会员 / 内容 / 播放节点都在这里，不用一个个切过去看。
        切当前作用域在顶栏，服与线路的归属在「服务器与线路」页按范围看。
      -->
      <SectionCard
        v-if="realms?.realms.length"
        title="各服概况"
        :icon="RealmIcon"
        :meta="`${realms.summary.total_realms} 个服 · 有效订阅 ${realms.summary.active_subscriptions} · 播放节点在线 ${realms.summary.nodes_online}/${realms.summary.nodes}`"
      >
        <template #actions>
          <RouterLink class="card-link" to="/servers">服务器与线路<ArrowRight :size="13" /></RouterLink>
          <RouterLink class="card-link" to="/realms">服管理<ArrowRight :size="13" /></RouterLink>
        </template>
        <div class="realm-rows">
          <RouterLink
            v-for="r in realms.realms"
            :key="r.id"
            to="/realms"
            class="realm-row"
            :class="{ current: r.id === realms.active_realm_id, off: !r.is_active }"
          >
            <div class="realm-name">
              <strong>{{ r.name }}</strong>
              <span class="au-badge au-badge-muted">{{ r.slug }}</span>
              <span v-if="r.id === realms.active_realm_id" class="au-badge au-badge-amber">当前服</span>
              <span v-if="r.is_default" class="au-badge au-badge-info">默认服</span>
            </div>
            <div class="realm-stats">
              <span>媒体库 <b>{{ r.stats.libraries }}</b></span>
              <span>条目 <b>{{ r.stats.items }}</b></span>
              <span>挂载 <b>{{ r.stats.mounts }}</b></span>
              <span>套餐 <b>{{ r.stats.plans }}</b></span>
              <span>有效订阅 <b>{{ r.stats.active_subscriptions }}</b></span>
              <span :class="{ warn: r.stats.nodes_online < r.stats.nodes }">
                节点 <b>{{ r.stats.nodes_online }}/{{ r.stats.nodes }}</b>
              </span>
              <span :class="{ warn: r.stats.pending_requests > 0 }">
                待审求片 <b>{{ r.stats.pending_requests }}</b>
              </span>
            </div>
          </RouterLink>
        </div>
      </SectionCard>

      <!-- 交易概览：每个数字都能点进它自己的明细页（带筛选深链） -->
      <section class="tile-grid" aria-label="交易概览">
        <StatTile
          to="/orders?status=paid"
          :icon="Wallet"
          label="累计营收"
          :value="fmtMoney(economy?.orders.revenue ?? 0)"
          tone="accent"
          :hint="`${economy?.orders.pending ?? 0} 笔待支付`"
        />
        <StatTile to="/invitations" :icon="Coins" label="积分存量" :value="economy?.total_points ?? 0" hint="全站用户持有" />
        <StatTile to="/users" :icon="CalendarCheck" label="今日签到" :value="economy?.checkins_today ?? 0" hint="人已签到" />
        <StatTile
          to="/codes?tab=exchange"
          :icon="TicketCheck"
          label="兑换码"
          :value="economy?.exchange_codes.used ?? 0"
          :suffix="`/ ${economy?.exchange_codes.total ?? 0}`"
          hint="已核销 / 已生成"
        />
        <StatTile to="/invitations" :icon="Gift" label="邀请关系" :value="economy?.invitations ?? 0" hint="累计成功邀请" />
      </section>

      <!-- 趋势 -->
      <SectionCard title="趋势" :icon="TrendingUp" :meta="`近 ${days} 天`">
        <template #actions>
          <div class="metric-tabs" role="tablist" aria-label="趋势指标">
            <button
              v-for="t in metricTabs"
              :key="t.key"
              class="metric-tab"
              role="tab"
              :aria-selected="metric === t.key"
              :class="{ active: metric === t.key }"
              @click="metric = t.key"
            >
              <span class="metric-dot" :style="{ background: t.color }" aria-hidden="true" />
              {{ t.label }}
            </button>
          </div>
          <el-radio-group v-model="days" size="small">
            <el-radio-button :value="7">7 天</el-radio-button>
            <el-radio-button :value="14">14 天</el-radio-button>
            <el-radio-button :value="30">30 天</el-radio-button>
          </el-radio-group>
        </template>

        <div class="trend-total">
          <span class="trend-total-label">近 {{ days }} 天合计</span>
          <span class="trend-total-value" :style="{ color: currentMetric.color }">
            {{ metric === 'revenue' ? fmtMoney(trend?.totals.revenue ?? 0) : (trend?.totals[metric] ?? 0) }}
          </span>
        </div>

        <div class="chart-wrap" v-loading="trendLoading">
          <svg class="chart" :viewBox="`0 0 ${CHART_W} ${CHART_H}`" preserveAspectRatio="none" role="img" :aria-label="`${currentMetric.label}趋势`">
            <defs>
              <linearGradient :id="`grad-${metric}`" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" :style="{ stopColor: currentMetric.color, stopOpacity: 0.28 }" />
                <stop offset="100%" :style="{ stopColor: currentMetric.color, stopOpacity: 0 }" />
              </linearGradient>
            </defs>
            <path :d="areaPath" :fill="`url(#grad-${metric})`" />
            <path
              :d="linePath"
              fill="none"
              :style="{ stroke: currentMetric.color }"
              stroke-width="2"
              vector-effect="non-scaling-stroke"
              stroke-linejoin="round"
              stroke-linecap="round"
            />
          </svg>
          <div class="chart-axis">
            <span v-for="(l, i) in axisLabels" :key="l + i">{{ l }}</span>
          </div>
        </div>
      </SectionCard>

      <!-- 媒体服务运行态：把媒体库与扫描状态放到首页，而不是让管理员逐页排查 -->
      <SectionCard
        v-if="libraries.length"
        title="媒体服务"
        :icon="Film"
        :meta="`条目 ${overview?.emby.total_items ?? 0}`"
      >
        <template #actions>
          <RouterLink to="/emby" class="card-link">管理媒体库 <ArrowRight :size="13" /></RouterLink>
        </template>
        <div class="ops-list">
          <div v-for="library in libraries.slice(0, 5)" :key="library.id" class="ops-row">
            <div class="ops-main">
              <strong>{{ library.name }}</strong>
              <span>{{ library.item_count }} 个条目 · {{ libraryStatus(library) }}</span>
            </div>
            <span class="status-dot" :class="{ warning: library.is_scanning, danger: !library.is_enabled }" />
          </div>
        </div>
      </SectionCard>

      <!-- 排行榜 -->
      <section v-if="playback" class="two-col">
        <SectionCard title="用户播放排行" :icon="Trophy" meta="近 7 天">
          <EmptyState v-if="playback.user_ranking.length === 0" compact :icon="Users" title="暂无播放数据" description="有人播放后这里会出现排行；如果已有人在看却没数据，去「一键体检」查一下。" />
          <div v-for="(u, i) in playback.user_ranking" :key="u.username" class="rank-row">
            <span class="rank-index" :class="{ top: i < 3 }">{{ i + 1 }}</span>
            <span class="rank-name">{{ u.username }}</span>
            <span class="rank-value">{{ u.plays }} 次</span>
          </div>
        </SectionCard>

        <SectionCard title="热门内容" :icon="Flame" meta="近 7 天">
          <EmptyState v-if="playback.item_ranking.length === 0" compact :icon="Film" title="暂无播放数据" description="有人播放后这里会出现排行；如果已有人在看却没数据，去「一键体检」查一下。" />
          <div v-for="(it, i) in playback.item_ranking" :key="it.name" class="rank-row">
            <span class="rank-index" :class="{ top: i < 3 }">{{ i + 1 }}</span>
            <span class="rank-name">
              {{ it.name }}
              <span class="rank-type">{{ it.type === 'episode' ? '剧集' : it.type === 'movie' ? '电影' : it.type }}</span>
            </span>
            <span class="rank-value">{{ it.plays }} 次</span>
          </div>
        </SectionCard>
      </section>
    </template>
  </div>
</template>

<style scoped>
/* 页面节奏：区块之间统一 16px（各区块自己不再写 margin） */
.dashboard { gap: 16px; }
.dashboard :deep(.au-page-header) { margin-bottom: 4px; }

/* ===== 首屏骨架 ===== */
.dash-skeleton {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 180px), 1fr));
  gap: 12px;
}
.sk-tile { height: 104px; border-radius: var(--au-r-lg); }
.sk-wide { grid-column: 1 / -1; height: 240px; border-radius: var(--au-r-lg); }

/* ===== 网格 =====
 * 交付链固定五张卡：auto-fit + minmax(210px) 在 1280 宽（内容区约 990px）只排得下 4 张，
 * 第 5 张孤零零掉到第二行。改成按视口给列数：宽屏 5 列一行排完，平板 3+2，手机 2+2+1（末张通栏）。 */
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 12px;
}

/* 扫描/存储卡的 foot 是多行状态（\n 分隔）：按行拆开展示，单行卡不受影响 */
.kpi-grid :deep(.au-stat__hint) {
  white-space: pre-line;
}
@media (max-width: 1180px) {
  .kpi-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}

.tile-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
}

.two-col {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 320px), 1fr));
  gap: 16px;
}

.tile-badge { padding: 0 6px; font-size: 10.5px; margin-left: 2px; }
/* 非健康服务的状态文字较长：字号收一档，不和大数字抢权重 */
.svc-tile :deep(.au-stat__value) { font-size: 1.125rem; }

/* ===== 卡片头里的文字链接 ===== */
.card-link {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: var(--au-primary);
  font-size: 12px;
  text-decoration: none;
}
.card-link:hover { color: var(--au-text); }

/* ===== 待办条 ===== */
.todo-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding: 12px 16px;
  border-radius: var(--au-r-lg);
  border: 1px solid var(--au-border);
  background: var(--au-surface);
}

.todo-bar.clear { border-color: var(--au-success-border); }

.todo-lead {
  display: flex;
  align-items: center;
  gap: 8px;
  padding-right: 6px;
  font-family: var(--au-font-serif);
  font-size: 14px;
  font-weight: 700;
  color: var(--au-text);
}

.todo-bar.clear .todo-lead { color: var(--au-success); }

.todo-lead-count {
  font-family: var(--au-font-sans);
  background: var(--au-danger-soft);
  border: 1px solid var(--au-danger-border);
  color: var(--au-danger);
  border-radius: var(--au-r-full);
  padding: 0 8px;
  font-size: 12px;
  line-height: 20px;
}

.todo-pill {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  height: 32px;
  padding: 0 12px;
  border-radius: var(--au-r-full);
  border: 1px solid var(--au-border);
  background: var(--au-surface-2);
  color: var(--au-text-2);
  font-size: 12.5px;
  text-decoration: none;
  transition: border-color var(--au-fast) var(--au-ease), color var(--au-fast) var(--au-ease);
}

.todo-pill:hover { border-color: var(--au-border-strong); color: var(--au-text); }
.todo-pill strong { color: var(--au-text); font-size: 13.5px; font-variant-numeric: tabular-nums; }
.todo-pill.zero { opacity: 0.55; }
.todo-pill.zero strong { color: var(--au-text-3); }
.todo-pill.danger:not(.zero) strong { color: var(--au-danger); }
.todo-pill.warning:not(.zero) strong { color: var(--au-warning); }
.todo-pill.info:not(.zero) strong { color: var(--au-info); }
.todo-arrow { opacity: 0; transition: opacity var(--au-fast) var(--au-ease); }
.todo-pill:hover .todo-arrow { opacity: 1; }

/* ===== 多服运营（每个服一行，切服在顶栏）===== */
.realm-rows { display: flex; flex-direction: column; gap: 6px; }
.realm-row {
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  padding: 10px 12px; border-radius: var(--au-r-md);
  border: 1px solid var(--au-border); background: var(--au-bg-soft);
  text-decoration: none;
  transition: border-color var(--au-fast) var(--au-ease), background var(--au-fast) var(--au-ease);
}
.realm-row:hover { border-color: var(--au-border-strong); background: var(--au-surface-2); }
.realm-row.current { border-color: var(--au-primary-border); }
.realm-row.off { opacity: 0.6; }
.realm-name { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; min-width: min(190px, 100%); }
.realm-name strong { color: var(--au-text); font-size: 13px; }
.realm-stats { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; margin-left: auto; }
.realm-stats span { color: var(--au-text-3); font-size: 12px; }
.realm-stats b { color: var(--au-text-2); font-variant-numeric: tabular-nums; }
.realm-stats span.warn b { color: var(--au-warning); }

/* ===== 趋势 ===== */
.metric-tabs { display: flex; gap: 4px; flex-wrap: wrap; }

.metric-tab {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 28px;
  padding: 0 10px;
  border-radius: var(--au-r-full);
  border: 1px solid transparent;
  background: transparent;
  color: var(--au-text-3);
  font-size: 12px;
  cursor: pointer;
  transition: color var(--au-fast) var(--au-ease), border-color var(--au-fast) var(--au-ease),
    background var(--au-fast) var(--au-ease);
}

.metric-dot { width: 6px; height: 6px; border-radius: 50%; opacity: 0.5; }
.metric-tab:hover { color: var(--au-text); }
.metric-tab.active { color: var(--au-text); border-color: var(--au-border); background: var(--au-surface-2); font-weight: 600; }
.metric-tab.active .metric-dot { opacity: 1; }
.metric-tab:focus-visible { outline: 2px solid var(--au-border-focus); outline-offset: 2px; }

.trend-total { display: flex; align-items: baseline; gap: 10px; margin-bottom: 10px; }
.trend-total-label { font-size: 12px; color: var(--au-text-3); }
.trend-total-value { font-size: 1.5rem; font-weight: 700; font-variant-numeric: tabular-nums; }

.chart-wrap { position: relative; }
.chart { width: 100%; height: 160px; display: block; }

.chart-axis {
  display: flex;
  justify-content: space-between;
  font-size: 11px;
  color: var(--au-text-4);
  margin-top: 6px;
  font-variant-numeric: tabular-nums;
}

/* ===== 媒体服务 ===== */
.ops-list { display: flex; flex-direction: column; }
.ops-row { display: flex; align-items: center; gap: 12px; padding: 10px 0; border-bottom: 1px solid var(--au-border); }
.ops-row:last-child { border-bottom: none; }
.ops-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 3px; }
.ops-main strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; color: var(--au-text); }
.ops-main span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; color: var(--au-text-3); }
.status-dot { width: 8px; height: 8px; flex: 0 0 8px; border-radius: 50%; background: var(--au-success); box-shadow: 0 0 0 4px var(--au-success-soft); }
.status-dot.warning { background: var(--au-warning); box-shadow: 0 0 0 4px var(--au-warning-soft); }
.status-dot.danger { background: var(--au-danger); box-shadow: 0 0 0 4px var(--au-danger-soft); }

/* ===== 排行 ===== */
.rank-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 9px 0;
  border-bottom: 1px solid var(--au-border);
}
.rank-row:last-child { border-bottom: none; }

.rank-index {
  width: 24px;
  height: 24px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--au-r-sm);
  background: var(--au-surface-2);
  color: var(--au-text-3);
  font-size: 12px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.rank-index.top { background: var(--au-primary-soft); color: var(--au-primary); }

.rank-name { flex: 1; font-size: 13px; color: var(--au-text); min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rank-type { font-size: 11px; color: var(--au-text-4); margin-left: 6px; }
.rank-value { font-size: 12px; color: var(--au-text-2); font-variant-numeric: tabular-nums; }

/* ===== 手机 ===== */
@media (max-width: 640px) {
  .dashboard { gap: 12px; }
  .kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
  .kpi-grid > :last-child:nth-child(odd) { grid-column: 1 / -1; }
  /* min(100%, …)：极窄屏（≤320px）也不会被轨道下限撑出横向滚动 */
  .tile-grid { grid-template-columns: repeat(auto-fit, minmax(min(100%, 150px), 1fr)); gap: 10px; }
  .todo-bar { padding: 10px 12px; gap: 8px; }
  .todo-lead { width: 100%; padding-right: 0; }
  .chart { height: 132px; }
  /* 7-30 个日期标签在窄屏会挤成一团，隔一个显示一个 */
  .chart-axis span:nth-child(even) { display: none; }
  .trend-total-value { font-size: 1.25rem; }
  .two-col { grid-template-columns: 1fr; }
}
</style>
