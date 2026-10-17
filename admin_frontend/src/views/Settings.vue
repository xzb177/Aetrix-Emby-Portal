<script setup lang="ts">
/**
 * 系统设置
 *
 * v2.4.0 注册策略（开放 / 注册码 / 关闭 + 关闭提示文案）与经济配置分组
 * v2.5.2 付费墙分组（要求有效订阅 + 拦截提示文案）
 * v2.6.0 下载与设备风控分组（下载开关 / 设备上限 / 自动踢人 / 日志保留）
 * v2.19.0 **外部服务能力中心**：代理 / 人机验证 / 邮件与模板 / Telegram / AI 模型 / IP 归属地
 *
 * v2.42.10（Phase 5·危险操作独立 tab）：页面改成三个页签——「能力与服务」「运营参数」
 * 「**危险操作**」。前两个装的都是日常要改的东西，第三个装的是**会立刻改变线上数据、
 * 且大多不能撤销**的操作（手动恢复配额熔断器 / 清理登录与安全日志）。
 * 把它们从仪表盘、登录日志页面里搬到这里，是为了让「不可撤销」这件事
 * 在**一个地方**说清楚：先备份、再确认、再执行。原来两处的按钮改成深链指过来
 * （如 `/settings?tab=danger&op=logs`），**执行逻辑只在这里一份**。
 *
 * 能力中心的口径：**项目只提供能力，凭据一律由管理员自己填**。字段表由后端下发，
 * 这里只按类型渲染控件，所以后端加字段不需要改这个页面，也不会出现「界面漏了字段」。
 * 密钥字段后端只回 ******，留空即保持原值不变。
 */
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import {
  CalendarCheck, Coins, Globe, KeyRound, Layers, Mail, MapPin, Network, Palette, RefreshCw, Save,
  ShieldCheck, Send, Sparkles, TicketCheck, UserPlus, Wallet, Lock, Zap,
  DatabaseBackup, History, TriangleAlert, SlidersHorizontal, PlugZap, Rocket,
} from 'lucide-vue-next'
import { EmptyState, PageHeader, SectionCard } from '@/components/ui'
import { useBreakpoint } from '@/composables/useBreakpoint'
import {
  fetchRegistrationSettings, updateRegistrationSettings,
} from '@/api/admin'
import { useAuthStore } from '@/stores/auth'
import { useQueryFilter } from '@/composables/useQueryFilter'
import { useDangerOps } from '@/composables/useDangerOps'
import { fetchEconomySettings, updateEconomySettings, type EconomySettings } from '@/api/economy'
import {
  fetchHotlinkConfig, updateHotlinkConfig, type HotlinkConfigPayload,
  type HotlinkConfigGet,
} from '@/api/economy'
import {
  fetchCapabilities, fetchCapability, saveCapability, testCapability,
  type CapabilityCard, type CapabilityField, type CapabilityTestResult,
} from '@/api/capabilities'
import NoticePanel from '@/components/NoticePanel.vue'

const route = useRoute()
const auth = useAuthStore()
/** 手机上能力配置抽屉铺满整屏 */
const { isPhone } = useBreakpoint()
const capDrawerSize = computed(() => (isPhone.value ? '100%' : '520px'))

/** ==================== 页签（Phase 5） ==================== */

const TAB_KEYS = ['capabilities', 'params', 'danger']
const tab = ref<string>('capabilities')
// 深链：/settings?tab=danger（仪表盘 / 命令面板 / 其它页面的「去危险操作」都指这里）
useQueryFilter(tab, 'tab')
watch(
  tab,
  (value) => {
    // 非法值回默认页签：宁可回到第一页，也不要停在一个不存在的页签上（空白页）
    if (!TAB_KEYS.includes(value)) tab.value = 'capabilities'
  },
  { immediate: true },
)

type FieldType = 'bool' | 'int' | 'str' | 'secret'

interface Field {
  key: string
  label: string
  type: FieldType
  hint?: string
  suffix?: string
  /** 有值时渲染成下拉（type 仍是 str，保存走同一串行化分支） */
  choices?: Array<{ value: string; label: string }>
  /**
   * 高级调优项：收进卡内「高级调优」折叠区，默认收起。
   * 只放"调了也不影响主流程"的参数（窗口/超时/分块大小/惩罚幅度等），
   * 总开关与日常要改的核心值永远留在外面。
   */
  advanced?: boolean
}

interface Group {
  id: string
  title: string
  desc: string
  icon: unknown
  fields: Field[]
}

const GROUPS: Group[] = [
  {
    id: 'checkin',
    title: '每日签到',
    desc: '签到基础积分与连签加成规则（积分仅限公益服用户）',
    icon: CalendarCheck,
    fields: [
      { key: 'checkin_enabled', label: '启用签到', type: 'bool', hint: '关闭后用户端签到入口隐藏，已发放的积分不受影响' },
      { key: 'checkin_base_points', label: '基础积分', type: 'int', suffix: '积分/次', hint: '未设置随机范围时的默认值' },
      { key: 'checkin_base_min', label: '基础积分下限', type: 'int', suffix: '积分/次', hint: '随机范围下限，默认跟随基础积分', advanced: true },
      { key: 'checkin_base_max', label: '基础积分上限', type: 'int', suffix: '积分/次', hint: '随机范围上限，与下限相同=固定值', advanced: true },
      { key: 'checkin_streak_bonus', label: '连签加成', type: 'int', suffix: '积分/天', hint: '每多连签一天额外增加的积分', advanced: true },
      { key: 'checkin_streak_max_bonus', label: '加成上限', type: 'int', suffix: '积分', hint: '连签加成的封顶值', advanced: true },
      { key: 'checkin_penalty_pct', label: '惩罚概率', type: 'int', suffix: '%', hint: '0=关闭惩罚', advanced: true },
      { key: 'checkin_penalty_min', label: '惩罚下限', type: 'int', suffix: '积分', hint: '单次惩罚最少扣多少积分。推荐值：1', advanced: true },
      { key: 'checkin_penalty_max', label: '惩罚上限', type: 'int', suffix: '积分', hint: '单次惩罚最多扣多少积分。推荐值：3', advanced: true },
    ],
  },
  {
    id: 'vitality',
    title: '活力值',
    desc: '公益服活力值：每日 00:00 自动扣减，低于阈值限制观影，签到/积分可恢复',
    icon: Zap,
    fields: [
      { key: 'vitality_enabled', label: '启用活力值', type: 'bool', hint: '关闭后不再每日扣减，公益服账号不会因此被限制' },
      { key: 'vitality_max', label: '活力上限', type: 'int', suffix: '点', hint: '活力值上限，默认 14' },
      { key: 'vitality_daily_cost', label: '每日扣减', type: 'int', suffix: '点/天', hint: '每天 00:00 自动扣除' },
      { key: 'vitality_limit_threshold', label: '观影阈值', type: 'int', suffix: '点', hint: '低于此值限制观影' },
      { key: 'vitality_point_cost', label: '续活兑换率', type: 'int', suffix: '积分/点', hint: '1 点活力需要的积分数' },
      { key: 'vitality_watch_reward', label: '观影奖励', type: 'int', suffix: '点', hint: '每次有效观影奖励的活力值' },
      { key: 'vitality_daily_gain_limit', label: '每日获取上限', type: 'int', suffix: '点/天', hint: '每日通过签到/观影免费获取的上限（0=不限）', advanced: true },
      { key: 'vitality_archive_clamp', label: '归档钳制', type: 'int', suffix: '点', hint: '归档/退群时活力值钳制上限', advanced: true },
    ],
  },
  {
    id: 'exchange',
    title: '兑换码 · 求片',
    desc: '兑换开关与用户每日求片上限',
    icon: TicketCheck,
    fields: [
      { key: 'exchange_enabled', label: '启用兑换', type: 'bool', hint: '关闭后兑换码无法使用，求片功能一并关闭' },
      {
        key: 'media_seek_daily_limit',
        label: '每日求片上限',
        type: 'int',
        suffix: '条/天',
        hint: '用户端提交求片时的硬性限额，未设置时默认 5 条',
      },
    ],
  },
  {
    id: 'payment',
    title: '充值 · 支付',
    desc: '易支付兼容网关；留空的密钥项表示保持不变',
    icon: Wallet,
    fields: [
      { key: 'recharge_enabled', label: '启用充值', type: 'bool', hint: '关闭后用户端充值入口隐藏，已有余额不受影响' },
      { key: 'subscription_purchase_enabled', label: '启用订阅购买', type: 'bool', hint: '关闭后用户买不了新订阅，已有的订阅继续有效' },
      { key: 'payment_gateway_url', label: '网关地址', type: 'str', hint: '例如 https://pay.example.com/' },
      { key: 'payment_partner_id', label: '商户 ID', type: 'str', hint: '易支付后台「商户信息」里的商户号' },
      { key: 'payment_partner_key', label: '商户密钥', type: 'secret', hint: '已配置时显示为 ******，留空不修改' },
      { key: 'payment_qqpay_enabled', label: '启用 QQ 钱包', type: 'bool', hint: '网关配好后才生效：多一种收款方式' },
      { key: 'site_url', label: '站点地址', type: 'str', hint: '用于支付回调，必须是外网可访问地址' },
    ],
  },
  {
    id: 'paywall',
    title: '付费墙（会员门禁）',
    desc: '开启后，非会员无法播放与下载；管理员账号始终放行',
    icon: Lock,
    fields: [
      {
        key: 'subscription_required',
        label: '要求有效订阅',
        type: 'bool',
        hint: '关闭则所有人均可直接播放',
      },
      {
        key: 'subscription_gate_message',
        label: '拦截提示文案',
        type: 'str',
        hint: '留空使用默认：需要有效的会员订阅才能播放，请先开通会员',
      },
    ],
  },
  {
    id: 'invitation',
    title: '邀请返利',
    desc: '邀请奖励与下级消费返利比例',
    icon: UserPlus,
    fields: [
      { key: 'invitation_enabled', label: '启用邀请', type: 'bool', hint: '关闭后邀请链接失效，不再产生新的奖励和返利' },
      { key: 'invitation_reward_points', label: '邀请人奖励', type: 'int', suffix: '积分', hint: '每成功邀请一人，邀请人拿到的积分。推荐值：100' },
      { key: 'invitation_invitee_reward_points', label: '被邀请人奖励', type: 'int', suffix: '积分', hint: '新人用邀请链接注册时多拿的积分。推荐值：50', advanced: true },
      { key: 'invitation_rebate_percent', label: '消费返利', type: 'int', suffix: '%', hint: '下级充值/消费时邀请人可得的比例', advanced: true },
      {
        key: 'promotion_reward_enabled',
        label: '推广奖励',
        type: 'bool',
        hint: '邀请成功后在双向积分之外「另发」一笔；默认关闭，不打开就不会发',
      },
      {
        key: 'promotion_reward_type',
        label: '推广奖励类型',
        type: 'str',
        choices: [
          { value: 'balance', label: '余额（积分）' },
          { value: 'days', label: '有效期（天）' },
        ],
        hint: '选「有效期」时给邀请人叠加会员天数，归属当前服',
      },
      { key: 'promotion_reward_amount', label: '推广奖励·余额数值', type: 'int', suffix: '积分', hint: '类型为「余额」时生效；0 = 不发', advanced: true },
      { key: 'promotion_reward_days', label: '推广奖励·有效期天数', type: 'int', suffix: '天', hint: '类型为「有效期」时生效；0 = 不发', advanced: true },
    ],
  },
  {
    id: 'stream_accel',
    title: '流媒体加速',
    desc: '一键开启：强制域名访问（防 IP 直连） + 自动信任 CF 真实 IP + 播放地址统一用加速域名',
    icon: Rocket,
    fields: [
      { key: 'stream_accel_enabled', label: '启用加速', type: 'bool', hint: '打开后只能用加速域名访问，IP 直连会被拒绝' },
      {
        key: 'stream_accel_domain',
        label: '加速域名',
        type: 'str',
        hint: '例如 cdn.example.com，不带 https://；保存后最长 60 秒生效；开启后只能用该域名访问',
      },
    ],
  },
  {
    id: 'series_merge',
    title: '同剧合并',
    desc: '同部剧因标题虚词差异被拆成多条时自动合并（TMDB 编号 + 宽松标题识别，有生死守卫）；存量无 TMDB 编号的剧集也会尝试宽松重识别',
    icon: Layers,
    fields: [
      { key: 'series_merge_enabled', label: '启用同剧合并', type: 'bool', hint: '关闭后扫描宽松复用与自动合并全停，严格去重不受影响' },
      { key: 'series_merge_loose_enabled', label: '宽松通道', type: 'bool', hint: '按去虚词标题识别同剧；关掉只走 TMDB 编号通道' },
      { key: 'series_merge_require_year', label: '年份强校验', type: 'bool', hint: '打开时组合并要求年份齐全且相差不超过 1 年' },
      { key: 'series_merge_interval_sec', label: '合并间隔', type: 'int', suffix: '秒', hint: '最短 3600 秒；扫描完成后会自动提前跑一轮' },
      { key: 'series_merge_batch_limit', label: '每轮组数上限', type: 'int', hint: '单轮最多处理的合并组数' },
    ],
  },
]

/**
 * 分组归类：运营参数越加越多（签到 / 活力值 / 邀请 / 支付 / 预热…），平铺 9 块卡片很难找。
 * 按「管什么」分成三段，目录与正文用同一份归类；注册策略固定放在第一段开头。
 */
interface GroupSection {
  id: string
  title: string
  groups: Group[]
}
const SECTION_DEFS: Array<{ id: string; title: string; groups: string[] }> = [
  { id: 'growth', title: '注册与福利', groups: ['checkin', 'vitality', 'invitation', 'exchange'] },
  { id: 'commerce', title: '付费与支付', groups: ['payment', 'paywall'] },
  { id: 'playback', title: '播放与风控', groups: ['stream_accel'] },
  { id: 'library', title: '媒体库', groups: ['series_merge'] },
]
const SECTIONS: GroupSection[] = (() => {
  const used = new Set<string>()
  const sections = SECTION_DEFS.map((s) => ({
    id: s.id,
    title: s.title,
    groups: s.groups
      .map((id) => GROUPS.find((g) => g.id === id))
      .filter((g): g is Group => {
        if (g) used.add(g.id)
        return Boolean(g)
      }),
  }))
  // 新加的分组忘了归类时兜底放到「其它」，不至于从页面上消失
  const rest = GROUPS.filter((g) => !used.has(g.id))
  if (rest.length) sections.push({ id: 'other', title: '其它', groups: rest })
  return sections
})()

const loading = ref(true)
/** 运营参数读取失败：显示错误态，避免管理员对着空表单点保存 */
const paramsError = ref(false)
/** 分组目录横向滚动归零：移动端是横滑导航，数据重载后回到起点，避免首个标签停在被裁的位置 */
const groupNavRef = ref<HTMLElement | null>(null)
function resetGroupNavScroll() {
  nextTick(() => { if (groupNavRef.value) groupNavRef.value.scrollLeft = 0 })
}
const settings = reactive<EconomySettings>({})
const original = ref<EconomySettings>({})

/** 注册策略 */
const reg = reactive({
  mode: 'open',
  message: '',
  legacyCode: false,
  ratelimitEnabled: true,
  ratelimitMax: 5,
  ratelimitWindow: 3600,
})
const regFormRef = ref<FormInstance>()
/**
 * 注册策略的"干净"快照：load() 成功后同步，用于全局脏检查。
 * legacyCode 是展示态（DB 残留 code 的提示），不参与脏比较。
 */
const regOriginal = ref({
  mode: 'open',
  message: '',
  ratelimitEnabled: true,
  ratelimitMax: 5,
  ratelimitWindow: 3600,
})
const regDirty = computed(() =>
  reg.mode !== regOriginal.value.mode
  || reg.message !== regOriginal.value.message
  || reg.ratelimitEnabled !== regOriginal.value.ratelimitEnabled
  || reg.ratelimitMax !== regOriginal.value.ratelimitMax
  || reg.ratelimitWindow !== regOriginal.value.ratelimitWindow,
)
function snapshotReg() {
  regOriginal.value = {
    mode: reg.mode,
    message: reg.message,
    ratelimitEnabled: reg.ratelimitEnabled,
    ratelimitMax: reg.ratelimitMax,
    ratelimitWindow: reg.ratelimitWindow,
  }
}
const regRules: FormRules = {
  ratelimitMax: [
    { type: 'number', min: 1, max: 10000, message: '次数需在 1–10000 之间', trigger: 'blur' },
  ],
  ratelimitWindow: [
    { type: 'number', min: 60, max: 86400, message: '窗口需在 60–86400 秒之间', trigger: 'blur' },
  ],
  message: [{ max: 200, message: '提示最多 200 个字', trigger: 'blur' }],
}

const regModeHint = computed(() => {
  const map: Record<string, string> = {
    open: '任何人都可以直接注册',
    closed: '关闭注册入口，仅显示提示文案',
  }
  return map[reg.mode] || ''
})

/**
 * 字段是否被后端接收：GET /economy/settings 按白名单下发全部可写键，
 * 不在返回里的键 PUT 时会被静默丢弃（保存提示成功、值却永远回不来）。
 * 这类字段置灰并说明，且不进保存载荷。读取失败（original 为空）时不做判断。
 */
function isSupported(key: string): boolean {
  const keys = Object.keys(original.value)
  return !keys.length || Object.prototype.hasOwnProperty.call(original.value, key)
}

// ==================== 外部服务能力中心 ====================

const CAP_ICONS: Record<string, unknown> = {
  proxy: Network, captcha: ShieldCheck, mail: Mail,
  telegram: Send, ai: Sparkles, geoip: MapPin, branding: Palette,
}

/** 少数能力测试需要额外输入（收件地址 / 要查的 IP / 探测地址） */
const TEST_INPUTS: Record<string, { key: string; label: string; placeholder: string }> = {
  mail: { key: 'to', label: '收件地址', placeholder: '留空则发给发件地址' },
  geoip: { key: 'ip', label: '要查的 IP', placeholder: '默认 8.8.8.8' },
  proxy: { key: 'target', label: '探测地址', placeholder: '默认 https://www.gstatic.com/generate_204' },
}

const capabilities = ref<CapabilityCard[]>([])
const capsLoading = ref(true)
const capsError = ref(false)
const capGroups = computed(() => {
  const groups: { name: string; items: CapabilityCard[] }[] = []
  // 防御：API 返回异常时 capabilities 可能不是数组，避免整个页签白屏
  const list = Array.isArray(capabilities.value) ? capabilities.value : []
  for (const card of list) {
    if (!card || typeof card !== 'object') continue
    const groupName = (card as CapabilityCard).group || '其他'
    const found = groups.find((g) => g.name === groupName)
    if (found) found.items.push(card as CapabilityCard)
    else groups.push({ name: groupName, items: [card as CapabilityCard] })
  }
  return groups
})

const drawerOpen = ref(false)
const drawerSlug = ref('')
const drawerTitle = ref('')
const drawerDesc = ref('')
const drawerHint = ref('')
const drawerFields = ref<CapabilityField[]>([])
const drawerValues = reactive<Record<string, string>>({})
const drawerOriginal = ref<Record<string, string>>({})
const drawerTestLabel = ref('测试连接')
// 没有 test() 的能力（例如站点与品牌）不显示「测试」按钮——
// 点了只会回一句「不支持测试」的按钮，本质是界面在骗人
const drawerTestable = ref(true)
const capSaving = ref(false)
const capTesting = ref(false)
const testInput = reactive({ value: '' })
const testResult = ref<CapabilityTestResult | null>(null)

const drawerDirty = computed(() =>
  drawerFields.value.some((f) => String(drawerValues[f.key] ?? '') !== String(drawerOriginal.value[f.key] ?? ''))
)
const drawerTestInput = computed(() => TEST_INPUTS[drawerSlug.value])

function capStatus(card: CapabilityCard): { label: string; tone: 'ok' | 'warn' | 'idle' } {
  if (card.enabled) return { label: '已启用', tone: 'ok' }
  if (card.configured) return { label: '已配置 · 未启用', tone: 'warn' }
  return { label: '未配置', tone: 'idle' }
}

async function loadCapabilities() {
  capsLoading.value = true
  capsError.value = false
  try {
    const res = await fetchCapabilities()
    // 防御：后端返回格式异常时保证 capabilities 始终是数组，避免页签白屏
    const list = res && Array.isArray(res.capabilities) ? res.capabilities : []
    capabilities.value = list
    if (!list.length) {
      // 空数组不算错误，模板会显示"暂无可配置的外部能力"
      capsError.value = false
    }
  } catch {
    // 拦截器已提示
    capsError.value = !capabilities.value.length
  } finally {
    capsLoading.value = false
  }
}

async function openCapability(slug: string) {
  try {
    const detail = await fetchCapability(slug)
    drawerSlug.value = slug
    drawerTitle.value = detail.spec.title
    drawerDesc.value = detail.spec.desc
    drawerHint.value = detail.spec.docs_hint || ''
    drawerFields.value = detail.spec.fields
    drawerTestLabel.value = detail.item.test_label || '测试连接'
    drawerTestable.value = detail.item.testable !== false
    for (const key of Object.keys(drawerValues)) delete drawerValues[key]
    for (const f of detail.spec.fields) {
      drawerValues[f.key] = detail.values[f.key] ?? f.default ?? ''
    }
    drawerOriginal.value = { ...drawerValues }
    testResult.value = null
    testInput.value = ''
    drawerOpen.value = true
  } catch {
    // 拦截器已提示
  }
}

async function saveCurrentCapability() {
  capSaving.value = true
  try {
    await saveCapability(drawerSlug.value, { ...drawerValues })
    ElMessage.success(`「${drawerTitle.value}」已保存${drawerSlug.value === 'proxy' ? '，出站代理立即生效' : ''}`)
    drawerOriginal.value = { ...drawerValues }
    await Promise.all([loadCapabilities(), openCapability(drawerSlug.value), refreshEconomy()])
  } catch {
    // 拦截器已提示
  } finally {
    capSaving.value = false
  }
}

async function runCapabilityTest() {
  capTesting.value = true
  testResult.value = null
  try {
    const payload: Record<string, unknown> = {}
    if (drawerTestInput.value && testInput.value.trim()) {
      payload[drawerTestInput.value.key] = testInput.value.trim()
    }
    testResult.value = await testCapability(drawerSlug.value, payload)
  } catch {
    // 拦截器已提示
  } finally {
    capTesting.value = false
  }
}

/**
 * 只刷新运营参数的「生效值」，保留其它分组里还没保存的改动：
 * 以前保存任一分组都会整页 load()，把别的分组刚改的输入悄悄冲掉。
 * ``savedKeys`` 是刚保存的那批键，一律以服务端为准。
 */
async function refreshEconomy(savedKeys: string[] = []) {
  try {
    const econ = await fetchEconomySettings()
    const prev = original.value
    for (const [key, value] of Object.entries(econ.settings)) {
      const dirty = String(settings[key] ?? '') !== String(prev[key] ?? '')
      if (!dirty || savedKeys.includes(key)) settings[key] = value
    }
    original.value = { ...econ.settings }
  } catch {
    // 拦截器已提示；保留当前表单
  }
}

async function load() {
  loading.value = true
  paramsError.value = false
  try {
    const [econ, registration] = await Promise.all([
      fetchEconomySettings(),
      fetchRegistrationSettings(),
    ])
    Object.assign(settings, econ.settings)
    original.value = { ...econ.settings }
    // 注册码门禁已下线：DB 里残留的 "code" 一律按 open 展示，保存时后端也会拒绝 "code"
    reg.mode = registration.mode === 'code' ? 'open' : registration.mode
    reg.legacyCode = registration.mode === 'code'
    reg.message = registration.message || ''
    // 注册限流（C5）：后端带默认值下发，做类型保护后赋值
    reg.ratelimitEnabled = typeof registration.ratelimit_enabled === 'boolean'
      ? registration.ratelimit_enabled
      : true
    reg.ratelimitMax = typeof registration.ratelimit_max === 'number' && Number.isFinite(registration.ratelimit_max)
      ? Math.floor(registration.ratelimit_max)
      : 5
    reg.ratelimitWindow = typeof registration.ratelimit_window === 'number' && Number.isFinite(registration.ratelimit_window)
      ? Math.floor(registration.ratelimit_window)
      : 3600
    snapshotReg()
  } catch {
    // 错误提示由 HTTP 拦截器统一处理
    paramsError.value = true
  } finally {
    loading.value = false
    // 分组目录回到横向起点（移动端横滑导航）
    resetGroupNavScroll()
  }
}

onMounted(async () => {
  // 深链 /settings?tab=danger 进来时 tab 在挂载前就已是 danger，下面的 watch(tab) 不会触发
  if (tab.value === 'danger') loadDanger()
  await Promise.all([load(), loadCapabilities(), loadHotlink()])
  // Batch D 入口收敛：邀请页 / 客户端策略等用 hash 深链跳进来（如 #group-invitation）
  // R40：querySelector 直接塞 hash 会在畸形值时抛 SyntaxError；改用 getElementById
  if (route.hash) {
    await nextTick()
    const el = document.getElementById(route.hash.slice(1))
    el?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }
})

/** ==================== 防盗链卡片（独立 endpoint，非通用分组） ==================== */
/** 用独立接口而非 GROUPS 通用分组：TTL 需要前后端双重范围校验（60~86400），
 * 通用分组的 int 字段只有 min=0 的粗校验，装不下。 */
const hotlink = reactive({ enabled: true, play_sign_ttl: 900, allowed_referers: '' })
const hotlinkOriginal = reactive({ enabled: true, play_sign_ttl: 900, allowed_referers: '' })
const hotlinkLoading = ref(false)

function applyHotlink(data: HotlinkConfigGet) {
  hotlink.enabled = !!data.enabled
  hotlink.play_sign_ttl = Number(data.play_sign_ttl) || 900
  // 后端回包是归一化后的域名数组；编辑态存逗号分隔串
  hotlink.allowed_referers = Array.isArray(data.allowed_referers)
    ? data.allowed_referers.join(', ')
    : String(data.allowed_referers ?? '')
  Object.assign(hotlinkOriginal, hotlink)
}

async function loadHotlink() {
  hotlinkLoading.value = true
  try {
    applyHotlink(await fetchHotlinkConfig())
  } catch {
    // 错误提示由 HTTP 拦截器统一处理
  } finally {
    hotlinkLoading.value = false
  }
}

const hotlinkDirty = computed(() =>
  hotlink.enabled !== hotlinkOriginal.enabled
  || hotlink.play_sign_ttl !== hotlinkOriginal.play_sign_ttl
  || hotlink.allowed_referers !== hotlinkOriginal.allowed_referers,
)

/** 防盗链保存核心：返回是否成功，失败时拦截器/校验已提示。 */
async function saveHotlinkCore(): Promise<boolean> {
  // 前端先行校验：与后端 play_sign.write_config 的范围保持一致
  if (!Number.isInteger(hotlink.play_sign_ttl) || hotlink.play_sign_ttl < 60 || hotlink.play_sign_ttl > 86400) {
    ElMessage.error('播放签名有效期需在 60~86400 秒之间')
    return false
  }
  const payload: HotlinkConfigPayload = {
    enabled: hotlink.enabled,
    play_sign_ttl: hotlink.play_sign_ttl,
    allowed_referers: hotlink.allowed_referers,
  }
  try {
    applyHotlink(await updateHotlinkConfig(payload))
    return true
  } catch {
    // 拦截器已提示
    return false
  }
}

/** 运营参数页签顶部的分组跳转：分组多（注册 + 9 块 + 防盗链），先看目录再定位，不用一路滚 */
function scrollToGroup(id: string) {
  document.getElementById(`group-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

/**
 * 「重新载入」：当前页签用得上的都重拉一遍。
 * 有未保存的改动时先弹确认——以前这里是静默丢弃：改了东西点一下重载，
 * 别的卡片里刚改的内容就被 load() 的 Object.assign 悄悄冲掉了。
 */
async function reloadAll() {
  if (anyDirty.value) {
    try {
      await ElMessageBox.confirm(
        `有 ${dirtyCount.value} 处改动还没保存，重新载入会把它们丢掉。`,
        '重新载入会丢弃未保存的改动',
        { type: 'warning', confirmButtonText: '继续载入', cancelButtonText: '再想想' },
      )
    } catch {
      return
    }
  }
  loadCapabilities()
  load()
  loadHotlink()
  if (tab.value === 'danger') loadDanger()
}

function isDirty(group: Group): boolean {
  return group.fields.some(
    (f) => isSupported(f.key) && String(settings[f.key] ?? '') !== String(original.value[f.key] ?? ''),
  )
}

/** 高级折叠：每卡默认收起。el-collapse 的 v-model，按卡 id 存展开态。 */
const advancedOpen = reactive<Record<string, string[]>>({})
// 预置空数组：v-model 拿到 undefined 时 el-collapse 展开会异常，默认收起即 []
for (const g of GROUPS) {
  if (g.fields.some((f) => f.advanced)) advancedOpen[g.id] = []
}
const commonFields = (g: Group): Field[] => g.fields.filter((f) => !f.advanced)
const advancedFields = (g: Group): Field[] => g.fields.filter((f) => f.advanced)
const hasAdvanced = (g: Group): boolean => g.fields.some((f) => f.advanced)

/**
 * 全局脏检查：运营参数页签里"改了没存"的地方总数。
 * 经济分组 / 注册策略 / 防盗链三处各自独立 endpoint，这里只计数，
 * 保存时 saveAll 按顺序逐个调。
 */
const dirtyGroups = computed(() => GROUPS.filter((g) => isDirty(g)))
const dirtyCount = computed(() =>
  dirtyGroups.value.length + (regDirty.value ? 1 : 0) + (hotlinkDirty.value ? 1 : 0),
)
const anyDirty = computed(() => dirtyCount.value > 0)
const savingAll = ref(false)

/** 经济分组保存核心：返回是否成功，失败时拦截器已提示。 */
async function saveGroupCore(group: Group): Promise<boolean> {
  const payload: EconomySettings = {}
  for (const f of group.fields) {
    if (!isSupported(f.key)) continue
    const raw = String(settings[f.key] ?? '').trim()
    if (f.type === 'int') {
      const n = Number.parseInt(raw === '' ? '0' : raw, 10)
      payload[f.key] = String(Number.isFinite(n) && n >= 0 ? n : 0)
    } else if (f.type === 'bool') {
      payload[f.key] = raw === 'false' ? 'false' : 'true'
    } else {
      payload[f.key] = raw
    }
  }
  try {
    await updateEconomySettings(payload)
    await refreshEconomy(Object.keys(payload))
    return true
  } catch {
    // 拦截器已提示
    return false
  }
}

const paymentReady = computed(() => {
  const url = String(settings.payment_gateway_url || '')
  const pid = String(settings.payment_partner_id || '')
  const keySet = String(settings.payment_partner_key || '') === '******'
  return Boolean(url && pid && keySet)
})

/** 注册策略保存核心：返回是否成功，校验失败/接口失败时已提示。 */
async function saveRegistrationCore(): Promise<boolean> {
  try {
    await regFormRef.value?.validate()
  } catch {
    return false
  }
  // 注册限流前端校验（C5）——保留作为双保险
  const rlMax = Number.parseInt(String(reg.ratelimitMax), 10)
  const rlWindow = Number.parseInt(String(reg.ratelimitWindow), 10)
  if (!Number.isFinite(rlMax) || rlMax < 1) {
    ElMessage.error('窗口内最大注册次数必须大于等于 1')
    return false
  }
  if (!Number.isFinite(rlWindow) || rlWindow < 60) {
    ElMessage.error('限流窗口必须大于等于 60 秒')
    return false
  }
  try {
    await updateRegistrationSettings({
      mode: reg.mode,
      message: reg.message,
      ratelimit_enabled: reg.ratelimitEnabled,
      ratelimit_max: rlMax,
      ratelimit_window: rlWindow,
    })
    reg.legacyCode = false
    snapshotReg()
    return true
  } catch {
    // 拦截器已提示
    return false
  }
}

/**
 * 全局保存：把运营参数页签里所有"改了没存"的地方一次存完。
 * 三处走各自的独立 endpoint（注册策略带表单校验 → 经济分组 → 防盗链带范围校验），
 * 某处失败不影响其它继续存，最后汇总报一次，不再逐个弹成功提示。
 */
async function saveAll() {
  if (!anyDirty.value || savingAll.value) return
  savingAll.value = true
  const failed: string[] = []
  let saved = 0
  try {
    if (regDirty.value) {
      if (await saveRegistrationCore()) saved += 1
      else failed.push('注册策略')
    }
    for (const g of dirtyGroups.value) {
      if (await saveGroupCore(g)) saved += 1
      else failed.push(g.title)
    }
    if (hotlinkDirty.value) {
      if (await saveHotlinkCore()) saved += 1
      else failed.push('防盗链')
    }
  } finally {
    savingAll.value = false
  }
  if (failed.length) {
    ElMessage.error(`「${failed.join('、')}」没存上，其他 ${saved} 处已保存，可再点一次全部保存重试`)
  } else {
    ElMessage.success(`已全部保存（${saved} 处）`)
  }
}

/**
 * 放弃：把运营参数页签里所有未保存的改动丢掉，回到服务端当前值。
 * load() 会同时重置经济分组与注册策略，loadHotlink() 重置防盗链。
 */
async function discardAll() {
  if (!anyDirty.value) return
  try {
    await ElMessageBox.confirm(
      '放弃后，你刚才改的、还没保存的内容都会丢掉，页面回到服务端当前的值。',
      '放弃未保存的改动？',
      { type: 'warning', confirmButtonText: '放弃改动', cancelButtonText: '再想想' },
    )
  } catch {
    return
  }
  load()
  loadHotlink()
}

// ==================== 危险操作（Phase 5） ====================

/** 只读审计角色：服务端会拒，这里先置灰并说清原因（口径同导航上的锁标记） */
const canWrite = computed(() => auth.admin?.can_write !== false)
const WRITE_HINT = '当前账号是只读审计角色：可以看，不能执行写操作'

/** 逻辑与确认文案都在 useDangerOps 里（仪表盘 / 客户端策略 / 登录日志共用同一份） */
const dangerOps = useDangerOps()
const {
  backup: backupInfo,
  busy: dangerBusy,
  loadState: loadDanger,
  loading: dangerLoading,
} = dangerOps
const {
  backupNow: doBackup,
  purgeLogs: doPurgeLogs,
} = dangerOps

/** 保留天数：0 = 清空全部登录与安全日志 */
const purgeDays = ref(90)
/** 从别的页面深链过来时高亮对应卡片（?op=logs） */
const TAB_OPS = ['logs']
const target = ref('')

// 切到危险操作页签时才拉数据
watch(tab, (value) => {
  if (value === 'danger') loadDanger()
})

/** 深链定位：?op=xxx → 高亮对应卡片并滚过去（其它页面「去危险操作」的落点） */
function scrollToTarget(key: string) {
  document.getElementById(`danger-${key}`)?.scrollIntoView({ block: 'center' })
}

watch(
  () => route.query.op,
  (op) => {
    const key = Array.isArray(op) ? (op[0] ?? '') : (op ?? '')
    target.value = TAB_OPS.includes(key) ? key : ''
    if (!target.value) return
    nextTick(() => scrollToTarget(target.value))
    // el-tabs 的页签内容是懒渲染的：首次切过去时 DOM 可能还没挂上，滚不到就再试一次
    window.setTimeout(() => scrollToTarget(target.value), 150)
    // 高亮只用来「指给你看这是哪一张」，停几秒就淡掉（否则一直亮着反而像出错了）
    window.setTimeout(() => { target.value = '' }, 4000)
  },
  { immediate: true },
)
</script>

<template>
  <div class="admin-page settings-page">
    <PageHeader
      eyebrow="系统与审计"
      title="系统设置"
      description="外部服务能力、运营参数与危险操作；修改后立即对用户端生效。"
    >
      <template #actions>
        <el-button @click="reloadAll">
          <RefreshCw :size="14" class="btn-ico" />重新载入
        </el-button>
      </template>
    </PageHeader>

    <!-- Phase 5：日常要改的放前两个页签，不可撤销的独立一页 -->
    <el-tabs v-model="tab" class="settings-tabs">
      <el-tab-pane name="capabilities">
        <template #label>
          <span class="tab-label"><Globe :size="13" />能力与服务</span>
        </template>

        <div class="settings-body">
          <!-- ==================== 外部服务能力 ==================== -->
          <NoticePanel
            title="外部服务能力说明"
            summary="凭据由管理员自行填写，填入后即可测试连接"
            :icon="Zap"
            storage-key="settings-capabilities"
          >
            <p class="capability-note">
              能力由面板提供，<strong>凭据全部由你自己填</strong>：各家的 API Key / 站点密钥 / 代理地址 / Bot Token
              都由本项目之外的账号体系签发，填进来即可用，每个能力都能当场「测试连接」。
            </p>
          </NoticePanel>

          <div v-if="capsLoading && !capabilities.length" class="cap-skeleton" aria-busy="true" aria-label="加载中">
            <div v-for="n in 6" :key="n" class="au-skeleton sk-cap" />
          </div>

          <EmptyState
            v-else-if="capsError"
            :icon="PlugZap"
            title="能力列表读取失败"
            description="可能是网络或后端暂时不可用。"
          >
            <template #actions><el-button size="small" @click="loadCapabilities">重试</el-button></template>
          </EmptyState>

          <EmptyState
            v-else-if="!capabilities.length"
            :icon="PlugZap"
            title="暂无可配置的外部能力"
            description="后端没有下发任何能力项。"
          />

          <div v-else class="cap-flow" :class="{ 'is-refreshing': capsLoading }">
            <section v-for="g in capGroups" :key="g.name" class="cap-section">
              <h2 class="au-eyebrow cap-group-title">{{ g.name }}</h2>
              <div class="cap-grid">
                <button
                  v-for="card in g.items"
                  :key="card.slug"
                  type="button"
                  class="cap-card"
                  :class="capStatus(card).tone"
                  @click="openCapability(card.slug)"
                >
                  <div class="cap-card-top">
                    <span class="cap-icon">
                      <component :is="CAP_ICONS[card.slug] || Globe" :size="18" />
                    </span>
                    <span
                      class="au-badge"
                      :class="capStatus(card).tone === 'ok' ? 'au-badge-green' : capStatus(card).tone === 'warn' ? 'au-badge-amber' : 'au-badge-muted'"
                    >{{ capStatus(card).label }}</span>
                  </div>
                  <h3 class="cap-title">{{ card.title }}</h3>
                  <p>{{ card.desc }}</p>
                  <span class="cap-more">配置与测试 →</span>
                </button>
              </div>
            </section>
          </div>
        </div>
      </el-tab-pane>

      <el-tab-pane name="params">
        <template #label>
          <span class="tab-label"><SlidersHorizontal :size="13" />运营参数</span>
        </template>

        <div v-if="loading && !Object.keys(original).length" class="params-skeleton" aria-busy="true" aria-label="加载中">
          <div class="au-skeleton sk-nav" />
          <div v-for="n in 3" :key="n" class="au-skeleton sk-card" />
        </div>

        <div v-else class="settings-body" :class="{ 'is-refreshing': loading }">
          <el-alert
            v-if="paramsError"
            type="error"
            show-icon
            :closable="false"
            title="运营参数读取失败"
            description="下面的表单可能不是当前生效值，请先点右上角「重新载入」再修改。"
          />

          <!-- 分组目录：按三段归类一眼看全，带未保存标记，点击跳转；滚动时吸顶 -->
          <!-- 吸顶层与横向滚动层分离：sticky 元素自身不再是滚动容器，避免移动端吸顶态滚动原点偏移 -->
          <div class="group-nav-sticky">
          <!-- 全局保存条：运营参数页签里改了任何一处，只在这里点一次就全存（注册策略/经济分组/防盗链各自独立 endpoint，按顺序逐个存） -->
          <div v-if="anyDirty" class="savebar" role="status">
            <span class="savebar-text">有 <strong>{{ dirtyCount }}</strong> 处改动还没保存</span>
            <span class="savebar-actions">
              <el-button size="small" @click="discardAll">放弃</el-button>
              <el-button size="small" type="primary" :loading="savingAll" @click="saveAll">
                <Save :size="14" class="btn-ico" />全部保存
              </el-button>
            </span>
          </div>
          <nav ref="groupNavRef" class="group-nav" aria-label="运营参数分组">
            <div v-for="sec in SECTIONS" :key="sec.id" class="group-nav-sec">
              <span class="group-nav-label">{{ sec.title }}</span>
              <button
                v-if="sec.id === 'growth'"
                type="button"
                class="group-chip"
                @click="scrollToGroup('registration')"
              >
                <KeyRound :size="13" />注册策略
              </button>
              <button
                v-for="g in sec.groups"
                :key="g.id"
                type="button"
                class="group-chip"
                :class="{ 'is-dirty': isDirty(g) }"
                @click="scrollToGroup(g.id)"
              >
                <component :is="g.icon" :size="13" />{{ g.title }}
                <span v-if="hasAdvanced(g)" class="chip-adv" title="含高级调优项，默认收起">高级</span>
                <span v-if="isDirty(g)" class="chip-dot" aria-label="有未保存的改动" />
              </button>
              <button
                v-if="sec.id === 'playback'"
                type="button"
                class="group-chip"
                :class="{ 'is-dirty': hotlinkDirty }"
                @click="scrollToGroup('hotlink')"
              >
                <ShieldCheck :size="13" />防盗链
                <span v-if="hotlinkDirty" class="chip-dot" aria-label="有未保存的改动" />
              </button>
            </div>
          </nav>
          </div>

          <template v-for="sec in SECTIONS" :key="sec.id">
          <h2 :id="`section-${sec.id}`" class="au-eyebrow section-title">{{ sec.title }}</h2>

          <!-- 注册策略 -->
          <SectionCard
            v-if="sec.id === 'growth'"
            id="group-registration"
            title="注册策略"
            :icon="KeyRound"
            description="决定新用户如何进入站点"
            class="group-card"
          >
            <template v-if="regDirty" #actions>
              <span class="au-badge au-badge-amber">未保存</span>
            </template>
            <el-form ref="regFormRef" :model="reg" :rules="regRules" label-position="top" class="field-form">
              <el-form-item label="注册模式">
                <div class="field-stack">
                  <el-radio-group v-model="reg.mode">
                    <el-radio-button value="open">开放注册</el-radio-button>
                    <el-radio-button value="closed">关闭注册</el-radio-button>
                  </el-radio-group>
                  <span class="field-hint">{{ regModeHint }}</span>
                  <!-- 注册码门禁已下线：DB 残留 "code" 自动归一为开放注册 -->
                  <el-alert
                    v-if="reg.legacyCode"
                    type="warning"
                    show-icon
                    :closable="false"
                    title="该站点的注册模式曾设为「注册码」，该模式已下线"
                    description="已自动按「开放注册」处理，点击保存后将正式更新为开放注册。"
                  />
                </div>
              </el-form-item>
              <el-form-item v-if="reg.mode === 'closed'" label="关闭提示" prop="message">
                <el-input v-model="reg.message" type="textarea" :rows="2" placeholder="展示给无法注册的用户…" />
              </el-form-item>
              <el-form-item label="新用户注册限流">
                <div class="field-stack">
                  <el-switch v-model="reg.ratelimitEnabled" active-text="启用" inactive-text="关闭" />
                  <span class="field-hint">按 IP 限制新用户注册频率，防批量注册。关闭后同一 IP 可无限制注册。</span>
                </div>
              </el-form-item>
              <el-form-item v-if="reg.ratelimitEnabled" label="窗口内最大注册次数" prop="ratelimitMax">
                <div class="field-stack">
                  <el-input-number v-model="reg.ratelimitMax" :min="1" :max="10000" :step="1" />
                  <span class="field-hint">同一 IP 在一个时间窗口内最多可注册次数（默认 5）</span>
                </div>
              </el-form-item>
              <el-form-item v-if="reg.ratelimitEnabled" label="限流时间窗口（秒）" prop="ratelimitWindow">
                <div class="field-stack">
                  <el-input-number v-model="reg.ratelimitWindow" :min="60" :max="86400" :step="60" />
                  <span class="field-hint">滑动窗口长度，单位秒（默认 3600，即 1 小时）</span>
                </div>
              </el-form-item>
            </el-form>
          </SectionCard>

          <!-- 支付状态提示：放在「付费与支付」段首，紧挨着要配的网关字段 -->
          <el-alert
            v-if="sec.id === 'commerce'"
            :type="paymentReady ? 'success' : 'warning'"
            show-icon
            :closable="false"
            :title="paymentReady
              ? '支付网关已配置完成，用户端可直接下单充值。'
              : '支付网关尚未配置齐全（需要网关地址 + 商户 ID + 商户密钥），用户端充值下单会提示未启用。'"
          />

          <!-- 经济配置分组 -->
          <SectionCard
            v-for="g in sec.groups"
            :id="`group-${g.id}`"
            :key="g.id"
            :title="g.title"
            :icon="g.icon"
            :description="g.desc"
            :tone="isDirty(g) ? 'accent' : 'default'"
            class="group-card"
          >
            <template v-if="isDirty(g)" #actions>
              <span class="au-badge au-badge-amber">未保存</span>
            </template>

            <el-form label-position="top" class="field-form field-grid">
              <el-form-item
                v-for="f in commonFields(g)"
                :key="f.key"
                :label="f.label"
                :class="{ 'is-wide': f.type === 'str' || f.type === 'secret', 'is-unsupported': !isSupported(f.key) }"
              >
                <div class="field-stack">
                  <div class="field-control">
                    <el-switch
                      v-if="f.type === 'bool'"
                      v-model="settings[f.key]"
                      active-value="true"
                      inactive-value="false"
                      :disabled="!isSupported(f.key)"
                    />
                    <el-input
                      v-else-if="f.type === 'int'"
                      v-model="settings[f.key]"
                      type="number"
                      min="0"
                      class="num-input"
                      :disabled="!isSupported(f.key)"
                    />
                    <el-select
                      v-else-if="f.choices"
                      v-model="settings[f.key]"
                      class="w-full"
                      :disabled="!isSupported(f.key)"
                    >
                      <el-option
                        v-for="c in f.choices"
                        :key="c.value"
                        :label="c.label"
                        :value="c.value"
                      />
                    </el-select>
                    <el-input
                      v-else
                      v-model="settings[f.key]"
                      :type="f.type === 'secret' ? 'password' : 'text'"
                      :show-password="f.type === 'secret'"
                      :placeholder="f.type === 'secret' ? '留空表示不修改' : ''"
                      :disabled="!isSupported(f.key)"
                    />
                    <span v-if="f.suffix" class="field-suffix">{{ f.suffix }}</span>
                  </div>
                  <span v-if="f.hint" class="field-hint">{{ f.hint }}</span>
                  <span v-if="!isSupported(f.key)" class="field-hint warn">
                    当前后端未开放此项的写入（不在 /economy/settings 返回里），保存会被忽略，已置灰。
                  </span>
                </div>
              </el-form-item>
            </el-form>

            <!-- 高级调优：默认收起。收的是"调了也不影响主流程"的参数（去重窗口/超时/分块大小/惩罚幅度等），总开关与日常要改的核心值永远留在外面 -->
            <el-collapse v-if="hasAdvanced(g)" v-model="advancedOpen[g.id]" class="advanced-collapse">
              <el-collapse-item name="adv">
                <template #title>
                  <span class="advanced-title">高级调优<span class="advanced-sub">（一般不用动）</span></span>
                </template>
                <el-form label-position="top" class="field-form field-grid">
                  <el-form-item
                    v-for="f in advancedFields(g)"
                    :key="f.key"
                    :label="f.label"
                    :class="{ 'is-wide': f.type === 'str' || f.type === 'secret', 'is-unsupported': !isSupported(f.key) }"
                  >
                    <div class="field-stack">
                      <div class="field-control">
                        <el-switch
                          v-if="f.type === 'bool'"
                          v-model="settings[f.key]"
                          active-value="true"
                          inactive-value="false"
                          :disabled="!isSupported(f.key)"
                        />
                        <el-input
                          v-else-if="f.type === 'int'"
                          v-model="settings[f.key]"
                          type="number"
                          min="0"
                          class="num-input"
                          :disabled="!isSupported(f.key)"
                        />
                        <el-select
                          v-else-if="f.choices"
                          v-model="settings[f.key]"
                          class="w-full"
                          :disabled="!isSupported(f.key)"
                        >
                          <el-option
                            v-for="c in f.choices"
                            :key="c.value"
                            :label="c.label"
                            :value="c.value"
                          />
                        </el-select>
                        <el-input
                          v-else
                          v-model="settings[f.key]"
                          :type="f.type === 'secret' ? 'password' : 'text'"
                          :show-password="f.type === 'secret'"
                          :placeholder="f.type === 'secret' ? '留空表示不修改' : ''"
                          :disabled="!isSupported(f.key)"
                        />
                        <span v-if="f.suffix" class="field-suffix">{{ f.suffix }}</span>
                      </div>
                      <span v-if="f.hint" class="field-hint">{{ f.hint }}</span>
                      <span v-if="!isSupported(f.key)" class="field-hint warn">
                        当前后端未开放此项的写入（不在 /economy/settings 返回里），保存会被忽略，已置灰。
                      </span>
                    </div>
                  </el-form-item>
                </el-form>
              </el-collapse-item>
            </el-collapse>
          </SectionCard>

          <!-- 防盗链：独立卡片（TTL 需要范围校验，走独立 endpoint），归在「播放与风控」段末 -->
          <SectionCard
            v-if="sec.id === 'playback'"
            id="group-hotlink"
            title="防盗链"
            :icon="ShieldCheck"
            description="播放签名校验总开关 + 签名有效期。播放链接带 15 分钟签名与来路检查，防止盗链。"
            :tone="hotlinkDirty ? 'accent' : 'default'"
            class="group-card"
          >
            <template v-if="hotlinkDirty" #actions>
              <span class="au-badge au-badge-amber">未保存</span>
            </template>

            <el-form label-position="top" class="field-form field-grid">
              <el-form-item label="启用防盗链">
                <div class="field-stack">
                  <div class="field-control">
                    <el-switch v-model="hotlink.enabled" />
                  </div>
                  <span class="field-hint">关闭后播放端点跳过播放签名校验（兼容老客户端）</span>
                </div>
              </el-form-item>
              <el-form-item label="播放签名有效期">
                <div class="field-stack">
                  <div class="field-control">
                    <el-input-number
                      v-model="hotlink.play_sign_ttl"
                      :min="60"
                      :max="86400"
                      :step="60"
                      controls-position="right"
                      class="num-input"
                    />
                    <span class="field-suffix">秒</span>
                  </div>
                  <span class="field-hint">uid/exp/sign 签名有效期，默认 900（15 分钟），范围 60~86400</span>
                </div>
              </el-form-item>
              <el-form-item label="Referer 白名单">
                <div class="field-stack">
                  <div class="field-control">
                    <el-input
                      v-model="hotlink.allowed_referers"
                      type="textarea"
                      :rows="2"
                      placeholder="如：cdn.example.com, player.example.com"
                      style="max-width: 420px"
                    />
                  </div>
                  <span class="field-hint">逗号分隔的域名；只有带 Referer 且主机不在名单里才会被 403。留空 = 不检查（放行所有）</span>
                </div>
              </el-form-item>
            </el-form>
          </SectionCard>
          </template>

          <p class="foot-note">
            <Coins :size="13" />
            积分、套餐与兑换码的明细请前往「订阅套餐」「兑换码」「邀请与积分」页面管理。
          </p>
        </div>
      </el-tab-pane>

      <!-- ==================== 危险操作（Phase 5） ==================== -->
      <el-tab-pane name="danger">
        <template #label>
          <span class="tab-label"><TriangleAlert :size="13" />危险操作</span>
        </template>

        <div v-loading="dangerLoading" class="settings-body">
          <!-- 先备份：不可撤销的操作之前，总得有个能回退的地方 -->
          <SectionCard title="动手之前，先备份一次" :icon="DatabaseBackup" class="danger-guard">
            <template #actions>
              <el-button
                type="primary"
                :loading="dangerBusy === 'backup'"
                :disabled="!canWrite"
                :title="canWrite ? '' : WRITE_HINT"
                @click="doBackup"
              >
                <DatabaseBackup :size="14" class="btn-ico" />立即备份
              </el-button>
            </template>
            <p class="danger-effect">
              下面的操作都会立刻改变线上数据，大多不可恢复。
              定时备份是每天一次，不等于「刚才那一刻的状态」。
            </p>
            <p class="danger-guard-meta">
              定时备份 {{ backupInfo?.enabled ? `已开启 · 每天 ${backupInfo.time} · 保留 ${backupInfo.keep_days} 天` : '未开启' }}
              · 上次执行 {{ backupInfo?.last_run || '—' }}
              · 已有 {{ backupInfo?.backups.length ?? 0 }} 份备份
            </p>
          </SectionCard>

          <el-alert
            v-if="!canWrite"
            type="warning"
            show-icon
            :closable="false"
            :title="`${WRITE_HINT}。下面的按钮已置灰，但内容仍然可读。`"
          />

          <!-- 2. 登录与安全日志 -->
          <SectionCard
            id="danger-logs"
            title="清理登录与安全日志"
            :icon="History"
            description="登录成功/失败、设备超限被拒、诱饵码触发封禁等风控事件的流水"
            class="danger-card"
            :class="{ 'is-target': target === 'logs' }"
          >
            <template #actions>
              <span class="au-badge au-badge-rose">删掉就查不到了</span>
            </template>
            <p class="danger-effect">
              保留 <strong>0</strong> 天 = 清空全部。删掉之后，风控与安全审计就查不到那段历史了。
              想让它自然收敛，应该去「安全与准入 → 客户端策略 → 下载与设备」改日志保留天数。
            </p>
            <template #footer>
              <div class="card-footer">
                <label class="field-suffix" for="purge-days">保留天数</label>
                <el-input-number
                  id="purge-days"
                  v-model="purgeDays"
                  :min="0"
                  :max="3650"
                  :controls="false"
                  class="purge-input"
                />
                <el-button
                  type="danger"
                  :disabled="!canWrite"
                  :loading="dangerBusy === 'logs'"
                  :title="canWrite ? '' : WRITE_HINT"
                  @click="doPurgeLogs(purgeDays)"
                >
                  清理日志
                </el-button>
              </div>
            </template>
          </SectionCard>

          <p class="foot-note">
            <TriangleAlert :size="13" />
            删除媒体库、删除服务器、删除挂载这类<strong>针对单个对象</strong>的操作仍然留在它们各自的页面上——
            那里才看得到要删的是什么；本页只收「跨对象、不可逆」的那几项。
          </p>
        </div>
      </el-tab-pane>
    </el-tabs>

    <!-- ==================== 能力配置抽屉 ==================== -->
    <el-drawer v-model="drawerOpen" :title="drawerTitle" :size="capDrawerSize">
      <div class="cap-drawer">
        <p class="cap-drawer-desc">{{ drawerDesc }}</p>
        <p v-if="drawerHint" class="field-hint cap-drawer-hint">{{ drawerHint }}</p>

        <el-form label-position="top" class="field-form">
          <el-form-item v-for="f in drawerFields" :key="f.key" :label="f.label">
            <div class="field-stack">
              <el-switch
                v-if="f.type === 'bool'"
                v-model="drawerValues[f.key]"
                active-value="true"
                inactive-value="false"
              />
              <el-select v-else-if="f.type === 'select'" v-model="drawerValues[f.key]">
                <el-option v-for="opt in f.options" :key="opt" :label="opt" :value="opt" />
              </el-select>
              <el-input
                v-else-if="f.type === 'int'"
                v-model="drawerValues[f.key]"
                type="number"
                class="num-input"
              />
              <el-input
                v-else
                v-model="drawerValues[f.key]"
                :type="f.type === 'secret' ? 'password' : 'text'"
                :show-password="f.type === 'secret'"
                :placeholder="f.type === 'secret' && drawerValues[f.key] === '******'
                  ? '已配置：保持 ****** 不修改，清空则删除' : (f.placeholder || '')"
              />
              <span v-if="f.hint" class="field-hint">{{ f.hint }}</span>
            </div>
          </el-form-item>

          <el-form-item v-if="drawerTestInput" :label="drawerTestInput.label">
            <el-input v-model="testInput.value" :placeholder="drawerTestInput.placeholder" />
          </el-form-item>
        </el-form>

        <el-alert
          v-if="testResult"
          :type="testResult.ok ? 'success' : 'error'"
          :title="testResult.ok ? '测试通过' : '测试未通过'"
          :description="testResult.message"
          :closable="false"
          show-icon
        />

        <div class="cap-drawer-footer">
          <el-button v-if="drawerTestable" :loading="capTesting" @click="runCapabilityTest">
            {{ drawerTestLabel }}
          </el-button>
          <el-button type="primary" :loading="capSaving" @click="saveCurrentCapability">
            <Save :size="14" class="btn-ico" />{{ drawerDirty ? '保存修改' : '保存' }}
          </el-button>
        </div>
        <p class="foot-note">
          <ShieldCheck :size="13" />密钥字段只以掩码回显（保持 ****** 不修改，清空则删除）<template v-if="drawerTestable">；测试会真实发起一次请求或投递，便于区分「密钥错」与「网络不通」</template>。
        </p>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.settings-page { gap: 16px; }
.btn-ico { margin-right: 4px; }
.w-full { width: 100%; }
.settings-body { display: flex; flex-direction: column; gap: 16px; }
.is-refreshing { opacity: 0.6; transition: opacity var(--au-fast) var(--au-ease); }

/* ==================== 页签 ==================== */
.tab-label { display: inline-flex; align-items: center; gap: 4px; }

/* ==================== 骨架 ==================== */
.cap-skeleton {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 12px;
}
.sk-cap { height: 132px; border-radius: var(--au-r-lg); }
.params-skeleton { display: flex; flex-direction: column; gap: 16px; }
.sk-nav { height: 40px; border-radius: var(--au-r-md); }
.sk-card { height: 220px; border-radius: var(--au-r-lg); }

/* ==================== 运营参数：分组目录 ==================== */
/* 吸顶层：只负责吸顶，不再是滚动容器（与横向滚动的 nav 分离，避免移动端吸顶态滚动原点偏移） */
.group-nav-sticky {
  position: sticky;
  top: calc(62px + env(safe-area-inset-top));
  z-index: 5;
  background: var(--au-bg);
  border-bottom: 1px solid var(--au-border);
}
.group-nav {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 18px;
  padding: 10px 0;
}
.group-nav-sec { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
/* 标签永不被压缩：窄屏下 chips 已是 flex: none，标签也要同等待遇，避免被挤到 pill 下面 */
.group-nav-label { flex-shrink: 0; font-size: 11.5px; color: var(--au-text-3); margin-right: 2px; white-space: nowrap; }
.section-title { margin: 8px 0 -4px; scroll-margin-top: 140px; }
.field-hint.warn { color: var(--au-warning); }
.group-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 12px;
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-full);
  background: var(--au-surface);
  color: var(--au-text-2);
  font: inherit;
  font-size: 12.5px;
  cursor: pointer;
  transition: border-color var(--au-fast) var(--au-ease), color var(--au-fast) var(--au-ease);
}
.group-chip:hover { border-color: var(--au-border-strong); color: var(--au-text); }
.group-chip:focus-visible { outline: 2px solid var(--au-border-focus); outline-offset: 2px; }
.group-chip svg { color: var(--au-primary); }
.group-chip.is-dirty { border-color: var(--au-primary-border); color: var(--au-text); }
.chip-dot {
  width: 6px;
  height: 6px;
  border-radius: var(--au-r-full);
  background: var(--au-primary);
}
/* 分组 chip 上的"高级"角标：这张卡里有默认收起的高级调优项 */
.chip-adv {
  font-size: 10.5px;
  line-height: 1;
  padding: 2px 6px;
  border-radius: var(--au-r-full);
  border: 1px solid var(--au-border);
  background: var(--au-surface-2);
  color: var(--au-text-3);
}
/* 全局保存条：吸顶容器内，有未保存改动时才出现；改了任何一处只在这里点一次 */
.savebar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 12px;
  margin-bottom: 6px;
  border: 1px solid var(--au-primary-border);
  border-radius: var(--au-r-md);
  background: var(--au-primary-soft);
}
.savebar-text { font-size: 13px; color: var(--au-text-2); }
.savebar-text strong { color: var(--au-primary); }
.savebar-actions { display: flex; align-items: center; gap: 8px; flex: none; }
/* 高级调优折叠：默认收起，收的是"调了也不影响主流程"的参数 */
.advanced-collapse { margin-top: 2px; border-top: 1px dashed var(--au-border); }
.advanced-collapse :deep(.el-collapse-item__header) { border-bottom: none; }
.advanced-title { font-size: 13px; font-weight: 600; color: var(--au-text-2); }
.advanced-sub { font-weight: 400; font-size: 12px; color: var(--au-text-3); margin-left: 4px; }
/* 跳转落点别被顶栏压住 */
.group-card { scroll-margin-top: 140px; }

/* ==================== 表单 ==================== */
.field-form :deep(.el-form-item) { margin-bottom: 18px; }
.field-form :deep(.el-form-item:last-child) { margin-bottom: 0; }
.field-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 0 24px;
}
.field-grid :deep(.el-form-item.is-wide) { grid-column: 1 / -1; max-width: 640px; }
.field-grid :deep(.el-form-item:last-child) { margin-bottom: 18px; }
.field-stack { display: flex; flex-direction: column; gap: 6px; width: 100%; min-width: 0; }
.field-control { display: flex; align-items: center; gap: 8px; min-width: 0; }
.field-suffix { font-size: 12px; color: var(--au-text-3); white-space: nowrap; }
.field-hint { font-size: 12px; color: var(--au-text-3); line-height: 1.6; }
.num-input { width: 160px; }

.card-footer {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.capability-note { margin: 0; line-height: 1.7; }
.capability-note strong { color: var(--au-text); font-weight: 600; }

.foot-note {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0;
  font-size: 12px;
  color: var(--au-text-3);
  padding: 0 2px 8px;
}
.foot-note strong { color: var(--au-text-2); }

/* ==================== 危险操作 ==================== */
/* 危险操作卡：左边一条警示色，与前两个页签的日常配置卡区分开 */
.danger-card { border-left: 3px solid var(--au-danger-border); scroll-margin-top: 80px; }
.danger-card.is-target {
  border-color: var(--au-danger);
  outline: 3px solid var(--au-danger-soft);
}
.danger-guard { border-color: var(--au-success-border); }
.danger-guard-meta { margin: 6px 0 0; font-size: 12px; color: var(--au-text-3); line-height: 1.6; }
.danger-effect {
  margin: 0;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--au-text-2);
}
.danger-effect strong { color: var(--au-text); font-weight: 600; }
/* 危险区隔离带：整份清空这类动作单独隔离，不跟轻量清理同排（Batch B） */
.danger-isolate {
  display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap;
  padding: 12px 14px; margin-top: 4px;
  border: 1px solid var(--danger-border); border-left: 3px solid var(--danger-border);
  border-radius: var(--au-r-md); background: var(--danger-bg);
}
.danger-isolate-text strong { display: block; font-size: 13px; color: var(--danger); margin-bottom: 4px; }
.danger-isolate-text span { font-size: 12px; color: var(--au-text-2); line-height: 1.7; }
.purge-input { width: 120px; }

/* ==================== 能力卡片 ==================== */
.cap-section { display: flex; flex-direction: column; gap: 10px; }
.cap-group-title { margin: 4px 0 2px; }

.cap-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 12px;
}

/*
  能力卡按列打包（v2.42.5）：单卡分组与相邻分组并排；分组本身不被拆开（break-inside: avoid），
  阅读顺序仍是分组的自然顺序。
*/
.cap-flow {
  display: block;
  column-width: 300px;
  column-gap: 12px;
}
.cap-flow .cap-section {
  display: block;
  break-inside: avoid;
  margin-bottom: 16px;
}
.cap-flow .cap-grid { display: block; }
.cap-flow .cap-card { margin-bottom: 12px; break-inside: avoid; }

.cap-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  width: 100%;
  text-align: left;
  padding: 16px;
  background: var(--au-surface);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-lg);
  cursor: pointer;
  font: inherit;
  color: inherit;
  transition: border-color var(--au-fast) var(--au-ease), background var(--au-fast) var(--au-ease);
}
.cap-card:hover { border-color: var(--au-border-strong); background: var(--au-surface-2); }
.cap-card:active { transform: scale(0.98); }
.cap-card:focus-visible { outline: 2px solid var(--au-border-focus); outline-offset: 2px; }
.cap-card.ok { border-color: var(--au-success-border); }
.cap-card.warn { border-color: var(--au-warning-border); }

.cap-card-top { display: flex; align-items: center; justify-content: space-between; }

.cap-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border-radius: var(--au-r-md);
  background: var(--au-primary-soft);
  color: var(--au-primary);
}

.cap-title { font-size: 15px; margin: 2px 0 0; color: var(--au-text); }
.cap-card p { font-size: 12px; color: var(--au-text-3); line-height: 1.6; margin: 0; }
.cap-more { font-size: 12px; color: var(--au-primary); margin-top: 4px; }

.cap-drawer { display: flex; flex-direction: column; gap: 12px; }
.cap-drawer-desc { font-size: 13px; color: var(--au-text-2); margin: 0; line-height: 1.6; }
.cap-drawer-hint { margin: 0; }
.cap-drawer :deep(.el-select) { width: 100%; }

.cap-drawer-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding-top: 12px;
  border-top: 1px solid var(--au-border);
}

/* 手机：数字输入铺满、保存按钮拉满整行 */
@media (max-width: 768px) {
  .num-input { width: 100%; }
  .field-grid { grid-template-columns: 1fr; }
  .card-footer { justify-content: stretch; }
  .card-footer :deep(.el-button) { flex: 1 1 auto; margin-left: 0; }
  .purge-input { flex: 1 1 100px; }
  .settings-body { gap: 12px; }
  .group-nav { flex-wrap: nowrap; overflow-x: auto; padding: 8px 0; scrollbar-width: none; }
  /* scrollbar-width 只 Firefox 认，WebKit 内核要这一行才真藏得住 */
  .group-nav::-webkit-scrollbar { display: none; }
  .group-nav-sec { flex-wrap: nowrap; flex: none; }
  .group-chip { flex: none; }
  .cap-grid { grid-template-columns: 1fr; }
  .cap-flow { column-width: auto; column-count: 1; }
  .cap-flow .cap-section { margin-bottom: 12px; }
  .cap-flow .cap-card { margin-bottom: 10px; }
  .cap-drawer-footer { justify-content: stretch; }
  .cap-drawer-footer :deep(.el-button) { flex: 1; margin-left: 0; }
  .danger-guard :deep(.au-section__head) { flex-wrap: wrap; }
}

</style>
