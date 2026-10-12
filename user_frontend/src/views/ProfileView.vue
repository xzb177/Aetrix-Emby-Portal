<script setup lang="ts">
import ProfileSubscriptions from '@/components/profile/ProfileSubscriptions.vue'
/**
 * 个人中心 — 账号页（v2.6.30 重设；v2.10.0 加入「正在播放」）
 *
 * 这里只放「账号自身」的事：身份卡（用户名 / 邮箱 / 注册时间 / 观看数据）、
 * Emby 账号（服务器地址 / 播放密码 / 多服 / 一键导入）、我的订阅、我的设备、
 * 正在播放（远程控制播放会话）、安全设置。
 *
 * 页面上不再铺功能磁贴：个人中心不是功能地图。全站入口由一份导航定义承担
 * （src/config/navigation.ts）——桌面在顶栏，低频入口在头像菜单，
 * 「个人中心 = 一堆按钮」的观感就此结束。
 *
 * 「正在播放」为什么在这里：它讲的是**控制**（哪台设备在放、能不能停），
 * 不是「我看过什么」——后者留在媒体库的观看记录分段。v2.10.0 从观看记录页迁过来。
 */
import { ref, computed, onMounted, onActivated } from 'vue'
import { useRouter } from 'vue-router'
import { useUserStore } from '@/stores/user'
import {
  authApi, embyApi, subscriptionApi, isExpiringSoon,
  type AuthUser, type AccountCard, type AccountRealmCard, type MySubscription, type WatchStats,
} from '@/api'
import { deviceApi, type MyDevice, type MyDevicesResponse } from '@/api/economy'
import { tgApi, type TgBindStatus } from '@/api/tg'
import TgBindModal from '@/components/TgBindModal.vue'
import { useToast } from '@/composables/useToast'
import { toastCopy, confirmCopy } from '@/constants/toastMessages'
import { toUserMessage } from '@/utils/toUserMessage'
import { useHaptics } from '@/composables/useHaptics'
import { useClipboard } from '@/composables/useClipboard'
import PlaybackSessions from '@/components/media/PlaybackSessions.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import {
  Mail, CalendarDays, Crown, Lock, KeyRound, LogOut, RefreshCw,
  Eye, EyeOff, Copy, Check, Sparkles, MonitorSmartphone, ChevronRight, TriangleAlert,
  MonitorPlay, LayoutDashboard, Settings2, Route, Cloud, HardDrive, Send,
} from 'lucide-vue-next'

const router = useRouter()
const userStore = useUserStore()
const toast = useToast()
const haptics = useHaptics()
const { copy } = useClipboard()

const user = computed(() => userStore.user as AuthUser | null)

// 身份卡上的首字母头像：比一个通用小人图标更像「我的账号」
const initial = computed(() => (user.value?.username || 'U').charAt(0).toUpperCase())

// ===== 数据 =====
const loading = ref(true)
const account = ref<AccountCard | null>(null)
const accountError = ref(false)
const stats = ref<WatchStats | null>(null)
const subscriptions = ref<MySubscription[]>([])
const cancellingSub = ref<number | null>(null)

const activeSub = computed(() => subscriptions.value.find(s => s.status === 'active' && s.days_left > 0) || null)

async function cancelSubscription(s: MySubscription) {
  if (!confirm(confirmCopy.cancelSubscription(s.plan_name))) return
  haptics.confirm()
  cancellingSub.value = s.id
  try {
    const res = await subscriptionApi.cancel(s.id)
    toast.success(res.message || toastCopy.subscriptionCancelled())
    await loadProfile(true)
  } catch (err: any) {
    toast.error(toUserMessage(err, 'cancelFailed'))
  } finally {
    cancellingSub.value = null
  }
}
// 临期：与后台到期提醒同口径（默认 7 天），续费入口就在钱包页
const expiringSoon = computed(() => isExpiringSoon(activeSub.value))
const watchHours = computed(() => {
  if (!stats.value) return '—'
  const h = Math.floor(stats.value.total_seconds / 3600)
  return h >= 1 ? `${h} 小时` : `${Math.floor(stats.value.total_seconds / 60)} 分钟`
})

const copiedField = ref('')
// Telegram 绑定
const showTgBind = ref(false)
const tgStatus = ref<TgBindStatus | null>(null)
async function loadTgStatus() {
  try {
    tgStatus.value = await tgApi.status()
  } catch { /* 未登录等情况忽略 */ }
}
async function handleTgBound() {
  await loadTgStatus()
  try { await userStore.fetchUser() } catch { /* 忽略 */ }
}
const showPlayPassword = ref(false)

// ===== 播放路径（2026-10 简化）：只有中转一条，无需选择 =====
// 视频经服务器转发，热门内容自动走本地缓存 + CF 边缘缓存。

const embyUsername = computed(() => account.value?.emby_username || user.value?.emby_username || user.value?.username || '—')
const serverUrl = computed(() => account.value?.base_url || window.location.origin)
const hasPlayPassword = computed(() => !!account.value?.has_password)

// 播放器一键导入（服务器地址 / 账号信息集中在本页，首页不再重复）
const importSchemes = computed(() => account.value?.import_schemes || {})
const hasSchemes = computed(() => Object.keys(importSchemes.value).length > 0)

// ===== 多服：我在这几个服各自的地址与会员（一个服一个会员）=====
const realmCards = computed(() => account.value?.realms || [])

// ===== 查看权限：服务器地址 / Emby 账号 / 线路只给有权限的人看 =====
// 付费服 = 有效订阅；公益服 = 花积分解锁。没权限时后端不下发地址，前端展示解锁入口
const viewPermission = computed(() => account.value?.view_permission || null)
const viewGranted = computed(() => !!viewPermission.value?.granted)
const unlocking = ref(false)
const showUnlockConfirm = ref(false)

async function handleUnlockView() {
  const vp = viewPermission.value
  if (!vp || unlocking.value) return
  unlocking.value = true
  try {
    const res = await embyApi.unlockView(account.value?.realm_id ?? undefined)
    toast.success(res.already ? '已经解锁过了' : '解锁成功')
    showUnlockConfirm.value = false
    account.value = await embyApi.getAccountCard()
  } catch (err: any) {
    toast.error(toUserMessage(err, 'unlockFailed'))
  } finally {
    unlocking.value = false
  }
}

// ===== 公益服（v2.7.0）：本服免费开放，不需要会员 =====
const isFreeRealm = computed(() => !!account.value?.is_free || userStore.isFreeRealm)
const realmNote = computed(
  () => account.value?.access_note || userStore.realmNote || '本服为公益服 · 免费开放：无需开通会员即可观看全库内容。',
)
// 公益服：没订阅也算“能看”，卡片不该写成「未开通」把用户吓回去
function realmState(r: AccountRealmCard) {
  if (r.is_free) return '公益服 · 免费开放'
  return r.subscribed ? `会员剩 ${daysLeft(r.end_date)} 天` : '未开通'
}

function daysLeft(end?: string | null): number {
  if (!end) return 0
  const diff = new Date(end).getTime() - Date.now()
  return diff > 0 ? Math.ceil(diff / 86400000) : 0
}

function openScheme(url: string) {
  window.location.href = url
}

/**
 * 复制服务器地址 / Emby 用户名 / 各服 base_url。
 *
 * 复制与提示交给 useClipboard（非安全上下文下 navigator.clipboard 不存在，
 * 它会降级到 execCommand 并给出可操作的失败提示）；这里只管成功后的勾选反馈。
 * field 形如 'server' / 'user' / 'realm-<id>'。
 */
const COPY_LABELS: Record<string, string> = {
  server: '服务器地址',
  user: '用户名',
}

const copyText = async (text: string, field: string) => {
  const label = COPY_LABELS[field] || '服务器地址'
  const ok = await copy(text, label)
  if (!ok) return
  copiedField.value = field
  setTimeout(() => { if (copiedField.value === field) copiedField.value = '' }, 1600)
}

// ===== 修改门户密码 =====
const showChangePwd = ref(false)
const pwdForm = ref({ oldPassword: '', newPassword: '', confirmPassword: '' })
const pwdLoading = ref(false)
const pwdError = ref('')

async function handleChangePassword() {
  const { oldPassword, newPassword, confirmPassword } = pwdForm.value
  if (!oldPassword || !newPassword || !confirmPassword) {
    pwdError.value = '请填写完整'
    return
  }
  if (newPassword.length < 6) {
    pwdError.value = '新密码至少 6 位'
    return
  }
  if (newPassword !== confirmPassword) {
    pwdError.value = '两次输入的新密码不一致'
    return
  }
  pwdError.value = ''
  pwdLoading.value = true
  try {
    await authApi.changePassword({ old_password: oldPassword, new_password: newPassword })
    toast.success(toastCopy.passwordChanged())
    showChangePwd.value = false
    pwdForm.value = { oldPassword: '', newPassword: '', confirmPassword: '' }
  } catch (err: any) {
    pwdError.value = toUserMessage(err, 'settingFailed')
  } finally {
    pwdLoading.value = false
  }
}

// ===== 设置播放密码 =====
const showSetPlayPwd = ref(false)
const playPwd = ref('')
const playPwdLoading = ref(false)

async function handleSetPlayPassword() {
  if (playPwd.value.length < 6) {
    toast.error('播放密码至少 6 位')
    return
  }
  playPwdLoading.value = true
  try {
    await embyApi.setPassword(playPwd.value)
    toast.success(toastCopy.playPasswordSet())
    showSetPlayPwd.value = false
    playPwd.value = ''
    account.value = await embyApi.getAccountCard()
  } catch (err: any) {
    toast.error(toUserMessage(err, 'settingFailed'))
  } finally {
    playPwdLoading.value = false
  }
}

// ===== 退出登录 =====
async function handleLogout() {
  await userStore.logout()
  router.push('/login')
}

// ===== 我的设备（第三方播放器登录设备）=====
// 设备上限由管理端配置；这里让用户自助查看与清理，避免顶到上限后无法登录
const devices = ref<MyDevice[]>([])
const deviceInfo = ref<MyDevicesResponse | null>(null)
const deviceLoading = ref(false)
const removingDevice = ref('')
const pendingRemove = ref('')

async function loadDevices() {
  deviceLoading.value = true
  try {
    const res = await deviceApi.mine()
    devices.value = res.devices
    deviceInfo.value = res
  } catch {
    // 静默：设备卡加载失败不影响页面
  } finally {
    deviceLoading.value = false
  }
}

async function handleRemoveDevice(device: MyDevice) {
  if (pendingRemove.value !== device.device_id) {
    pendingRemove.value = device.device_id
    window.setTimeout(() => {
      if (pendingRemove.value === device.device_id) pendingRemove.value = ''
    }, 4000)
    return
  }
  pendingRemove.value = ''
  removingDevice.value = device.device_id
  try {
    const res = await deviceApi.remove(device.device_id)
    toast.success(res.message || toastCopy.deviceRemoved())
    await loadDevices()
  } catch (err: any) {
    toast.error(toUserMessage(err, 'removeFailed'))
  } finally {
    removingDevice.value = ''
  }
}

function deviceAgo(iso?: string | null, isBlocked = false) {
  if (isBlocked) return '已禁用'
  if (!iso) return '—'
  const diff = Date.now() - new Date(iso).getTime()
  if (Number.isNaN(diff) || diff < 0) return '刚刚'
  const mins = Math.floor(diff / 60000)
  if (mins < 60) return `${mins} 分钟前`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours} 小时前`
  return `${Math.floor(hours / 24)} 天前`
}

function refreshAccount() {
  loading.value = true
  accountError.value = false
  embyApi.getAccountCard()
    .then(a => { account.value = a })
    .catch(() => { accountError.value = true })
    .finally(() => { loading.value = false })
}

// ===== 初始化 =====
// silent = true 时为 KeepAlive 切回 tab 的后台静默刷新：不碰 loading，
// 不闪骨架屏，数据到了直接更新视图
const hasLoaded = ref(false)
async function loadProfile(silent = false) {
  if (!silent) loading.value = true
  try {
    // 观看统计与订阅加载失败不阻塞页面（新用户可能无数据）
    const [accountCard, watchStats, subs] = await Promise.all([
      embyApi.getAccountCard(),
      embyApi.getStats().catch((): WatchStats | null => null),
      subscriptionApi.getMine().catch((): MySubscription[] => []),
    ])
    account.value = accountCard
    // 静默刷新失败时不降级：getStats/getMine 失败走 catch 给 null/[]，
    // 直接赋值会把好数据刷成"—"/空；首屏失败则保持旧行为（显示空态）
    if (!silent || watchStats) stats.value = watchStats
    if (!silent || subs.length) subscriptions.value = subs
    // 播放路径只有中转一条，无需加载偏好
  } catch {
    // 401 已由拦截器处理；账号卡加载失败且无旧数据时标记，供模板展示错误与重试
    // （静默刷新失败不覆盖已有数据）
    if (!account.value) accountError.value = true
  } finally {
    loading.value = false
    hasLoaded.value = true
  }
  loadDevices()
}

onMounted(() => {
  loadTgStatus()
  loadProfile()
})

// 从别的 tab 切回来（KeepAlive 缓存命中）：后台静默刷新，不闪骨架屏
onActivated(() => {
  if (hasLoaded.value) {
    loadProfile(true)
    loadTgStatus() // Bot 端绑定后切回本页，徽章不再过期
  }
})

function formatDate(iso?: string | null) {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleDateString('zh-CN', { year: 'numeric', month: 'long', day: 'numeric' })
  } catch {
    return iso
  }
}
</script>

<template>
  <div class="au-page profile-page">
    <!-- Hero 身份区：放映机票根式 -->
<section class="hero">
  <div class="hero-ambiance" aria-hidden="true"></div>
  <!-- 骨架屏：首屏加载时占位 -->
  <div v-if="loading" class="hero-skeleton" aria-hidden="true">
    <div class="au-skeleton sk-avatar"></div>
    <div class="sk-hero-main">
      <div class="au-skeleton sk-hero-name"></div>
      <div class="au-skeleton sk-hero-meta"></div>
    </div>
  </div>
  <div v-else>
  <div class="hero-top">
    <div class="hero-avatar" :class="{ 'is-vip': activeSub || user?.is_vip }">{{ initial }}</div>
    <div class="hero-main">
      <p class="micro-label">我的账号</p>
      <h1 class="hero-name">{{ user?.username || '用户' }}</h1>
      <p class="hero-sub">
        <span v-if="activeSub" class="ticket"><Crown :size="12" />{{ activeSub.plan_name }} · 剩 {{ activeSub.days_left }} 天</span>
        <span v-else-if="user?.is_vip" class="ticket"><Crown :size="12" />VIP</span>
      </p>
      <div class="hero-meta">
        <span class="meta-item"><Mail :size="13" />{{ user?.email || '未绑定邮箱' }}</span>
        <span class="meta-dot"></span>
        <span class="meta-item"><CalendarDays :size="13" />注册于 {{ formatDate(user?.created_at) }}</span>
      </div>
    </div>
  </div>
  <div class="hero-stats">
    <div class="hero-stat"><strong>{{ watchHours }}</strong><span>累计观看</span></div>
    <div class="hero-stat"><strong>{{ stats?.total_plays ?? '—' }}</strong><span>播放次数</span></div>
    <div class="hero-stat"><strong>{{ stats?.watched_items ?? '—' }}</strong><span>看过影片</span></div>
  </div>
  </div>
</section>

    <!-- 账号的两栏：左边是「怎么连播放器」，右边是「我的钱 / 我的设备 / 我的安全」 -->
    <div class="p-grid">
      <!-- Emby 账号 -->
      <section class="pane">
        <header class="pane-head">
          <h2 class="pane-title">
            <KeyRound :size="17" />
            Emby 账号
          </h2>
          <button class="icon-btn" title="刷新" @click="refreshAccount">
            <RefreshCw :size="15" :class="{ spinning: loading }" />
          </button>
        </header>

        <!-- 账号卡加载失败：给错误说明 + 重试，不再满屏显示"—" -->
        <div v-if="accountError && !account" class="account-error">
          <p class="account-error-text">账号信息加载失败，请检查网络后重试</p>
          <button class="btn ghost" type="button" @click="refreshAccount">重新加载</button>
        </div>

        <!-- 公益服：一进来就说清这个服不要钱、规则是什么，别让用户去找开通入口 -->
        <div v-if="isFreeRealm" class="rows">
          <div class="row">
            <span class="row-label">接入方式</span>
            <span class="row-value">
              <span class="badge free">公益服 · 免费开放</span>
            </span>
          </div>
          <div class="row">
            <span class="row-label">规则</span>
            <span class="row-value wrap">{{ realmNote }}</span>
          </div>
        </div>

        <!-- 有查看权限才展示服务器地址 / 账号 / 密码：没权限时后端也不下发 -->
        <template v-if="viewGranted">
        <div class="rows" :class="{ loading }">
          <div class="row">
            <span class="row-label">服务器</span>
            <span class="row-value mono">{{ serverUrl }}</span>
            <button class="copy-btn" title="复制" @click="copyText(serverUrl, 'server')">
              <Check v-if="copiedField === 'server'" :size="14" class="ok" />
              <Copy v-else :size="14" />
            </button>
          </div>
          <div class="row">
            <span class="row-label">用户名</span>
            <span class="row-value mono">{{ embyUsername }}</span>
            <button class="copy-btn" title="复制" @click="copyText(embyUsername, 'user')">
              <Check v-if="copiedField === 'user'" :size="14" class="ok" />
              <Copy v-else :size="14" />
            </button>
          </div>
          <div class="row">
            <span class="row-label">播放密码</span>
            <span class="row-value mono">
              <template v-if="hasPlayPassword">{{ showPlayPassword ? '已设置' : '••••••••' }}</template>
              <template v-else>未设置</template>
            </span>
            <button v-if="hasPlayPassword" class="copy-btn" :title="showPlayPassword ? '隐藏' : '查看'" @click="showPlayPassword = !showPlayPassword">
              <Eye v-if="showPlayPassword" :size="14" />
              <EyeOff v-else :size="14" />
            </button>
            <button class="text-btn" @click="showSetPlayPwd = true">
              {{ hasPlayPassword ? '修改' : '设置' }}
            </button>
          </div>
        </div>
        </template>

        <!-- 没查看权限：解锁入口（付费服用订阅，公益服用积分） -->
        <div v-else-if="viewPermission" class="unlock-card">
          <template v-if="viewPermission.realm_free">
            <p class="unlock-title">查看 Emby 账号与线路需要解锁</p>
            <p class="unlock-desc">
              花 <strong>{{ viewPermission.unlock_points }}</strong> 积分解锁查看权限，
              有效期 {{ viewPermission.unlock_days > 0 ? viewPermission.unlock_days + ' 天' : '永久' }}。
              你当前有 <strong>{{ viewPermission.points_balance }}</strong> 积分。
            </p>
            <button
              class="btn primary"
              :disabled="unlocking || viewPermission.points_balance < viewPermission.unlock_points"
              @click="showUnlockConfirm = true"
            >
              {{ viewPermission.points_balance < viewPermission.unlock_points ? '积分不足' : `花 ${viewPermission.unlock_points} 积分解锁` }}
            </button>
            <p class="unlock-hint">积分可通过每日签到、邀请好友等方式获得</p>
          </template>
          <template v-else>
            <p class="unlock-title">开通会员后可见</p>
            <p class="unlock-desc">服务器地址、Emby 账号与一键导入只向会员开放。</p>
            <button class="btn primary" @click="router.push('/store')">去开通会员</button>
          </template>
        </div>

        <!-- 多服：每个服一个地址与一份会员（没订阅的服后端不下发） -->
        <div v-if="realmCards.length > 1" class="sub-block">
          <span class="block-label">我的服</span>
          <div class="realm-list">
            <div v-for="r in realmCards" :key="r.id" class="realm-item">
              <div class="realm-head">
                <strong>{{ r.name }}</strong>
                <span v-if="r.is_free" class="badge free">公益服 · 免费开放</span>
                <span v-else-if="r.subscribed" class="badge ok">会员剩 {{ daysLeft(r.end_date) }} 天</span>
                <span v-else class="badge off">未开通</span>
              </div>
              <div v-if="r.view_granted" class="realm-url">
                <span class="mono">{{ r.base_url || '管理员还没填这个服的地址' }}</span>
                <button v-if="r.base_url" class="copy-btn" title="复制" @click="copyText(r.base_url, `realm-${r.id}`)">
                  <Check v-if="copiedField === `realm-${r.id}`" :size="13" class="ok" />
                  <Copy v-else :size="13" />
                </button>
              </div>
              <div v-else class="realm-url muted">
                <span>{{ r.is_free ? '花积分解锁后可见' : '开通会员后可见' }}</span>
              </div>
            </div>
          </div>
        </div>

        <div v-if="viewGranted && hasSchemes" class="sub-block">
          <span class="block-label">一键导入到客户端</span>
          <div class="scheme-btns">
            <button
              v-for="(url, name) in importSchemes"
              :key="name"
              class="scheme-btn"
              @click="openScheme(url)"
            >
              <Sparkles :size="13" />
              {{ name }}
            </button>
          </div>
        </div>

        <p v-if="viewGranted" class="pane-tip">
          播放密码用于 Emby 客户端登录，与门户密码相互独立；也可以用上面的按钮一键导入。
        </p>
        <p v-else-if="viewPermission?.expires_at" class="pane-tip">
          查看权限有效期至 {{ formatDate(viewPermission.expires_at) }}。
        </p>
      </section>

      <div class="p-side">
        <!-- 正在播放：讲的是「控制」（哪台设备在放、能不能停），不是「我看过什么」，
             所以它在这里，而历史留在媒体库的观看记录分段 -->
        <section class="pane">
          <header class="pane-head">
            <h2 class="pane-title">
              <MonitorPlay :size="17" />
              正在播放
            </h2>
          </header>
          <PlaybackSessions />
          <p class="pane-tip">远程结束播放只会终止会话，不会删除观看记录。</p>
        </section>
        <ProfileSubscriptions
          :active-sub="activeSub"
          :subscriptions="subscriptions"
          :expiring-soon="expiringSoon"
          :is-free-realm="isFreeRealm"
          :realm-note="realmNote"
          :cancelling-sub="cancellingSub"
          @cancel="cancelSubscription"
        />

        </section>

        <!-- 我的设备 -->
        <section class="pane">
          <header class="pane-head">
            <h2 class="pane-title">
              <MonitorSmartphone :size="17" />
              我的设备
              <span v-if="deviceInfo && deviceInfo.limit" class="count-chip">
                {{ deviceInfo.active_count }} / {{ deviceInfo.limit }}
              </span>
            </h2>
            <button class="icon-btn" title="刷新" @click="loadDevices">
              <RefreshCw :size="15" :class="{ spinning: deviceLoading }" />
            </button>
          </header>

          <EmptyState
            v-if="devices.length === 0"
            size="compact"
            title="还没有设备记录"
            description="用 Emby 客户端（Infuse / Forward 等）登录后会出现在这里"
          />
          <template v-else>
            <div v-for="d in devices" :key="d.device_id" class="dev-row">
              <div class="dev-main">
                <span class="dev-name">{{ d.name || '未命名设备' }}</span>
                <span class="dev-meta">
                  {{ d.client || '未知客户端' }}
                  <template v-if="d.app_version"> v{{ d.app_version }}</template>
                  · {{ deviceAgo(d.last_seen_at, d.is_blocked) }}
                  <template v-if="d.ip"> · {{ d.ip }}</template>
                </span>
              </div>
              <span v-if="d.is_blocked" class="badge off">已禁用</span>
              <span v-else-if="!d.is_online_recent" class="badge idle">已闲置</span>
              <span v-else class="badge ok">活跃</span>
              <button
                class="text-btn danger"
                :class="{ confirming: pendingRemove === d.device_id }"
                :disabled="removingDevice === d.device_id"
                @click="handleRemoveDevice(d)"
              >
                {{ pendingRemove === d.device_id ? '确认移除' : '移除' }}
              </button>
            </div>
          </template>

          <p class="pane-tip">
            <template v-if="deviceInfo && deviceInfo.limit">
              账号最多同时使用 {{ deviceInfo.limit }} 台设备（近 {{ deviceInfo.active_days }} 天内登录过）。
              {{ deviceInfo.auto_evict ? '超限时会自动停用最久未用的设备。' : '超限时请先移除不再使用的设备。' }}
            </template>
            <template v-else>移除后该设备登录状态立即失效，需重新输入账号密码。</template>
          </p>
        </section>


        <!-- 播放设置：只有中转一条路径，热门内容自动走缓存 -->
        <section class="pane">
          <header class="pane-head">
            <h2 class="pane-title">
              <Settings2 :size="17" />
              播放设置
            </h2>
          </header>
          <p class="line-only">
            <Route :size="14" />
            中转播放（视频经服务器转发，网盘凭据不下发到客户端；热门内容自动缓存）
          </p>
        </section>

        <!-- 安全设置 -->
        <section class="pane">
          <header class="pane-head">
            <h2 class="pane-title">
              <Lock :size="17" />
              安全设置
            </h2>
          </header>

          <div class="list">
            <button class="list-item" @click="showTgBind = true">
              <Send :size="16" class="list-icon" />
              <span class="list-text">绑定 Telegram</span>
              <span v-if="tgStatus?.bound" class="badge ok">已绑定</span>
              <span v-else class="badge off">未绑定</span>
              <ChevronRight class="list-arrow" :size="15" />
            </button>
            <button class="list-item" @click="showChangePwd = true">
              <Lock :size="16" class="list-icon" />
              <span class="list-text">修改登录密码</span>
              <ChevronRight class="list-arrow" :size="15" />
            </button>
            <a v-if="user?.is_staff" href="/admin/" class="list-item list-link">
              <LayoutDashboard :size="16" class="list-icon" />
              <span class="list-text">管理后台</span>
              <ChevronRight class="list-arrow" :size="15" />
            </a>
            <button class="list-item danger" @click="handleLogout">
              <LogOut :size="16" class="list-icon" />
              <span class="list-text">退出登录</span>
              <ChevronRight class="list-arrow" :size="15" />
            </button>
          </div>
        </section>
      </div>
    </div>

    <TgBindModal v-model="showTgBind" @bound="handleTgBound" />

    <!-- 修改密码弹窗 -->
    <div v-if="showChangePwd" class="modal-mask" @click.self="showChangePwd = false">
      <div class="modal">
        <h3 class="modal-title">修改登录密码</h3>
        <div class="field">
          <label class="field-label">当前密码</label>
          <input v-model="pwdForm.oldPassword" type="password" autocomplete="current-password" />
        </div>
        <div class="field">
          <label class="field-label">新密码</label>
          <input v-model="pwdForm.newPassword" type="password" autocomplete="new-password" />
        </div>
        <div class="field">
          <label class="field-label">确认新密码</label>
          <input v-model="pwdForm.confirmPassword" type="password" autocomplete="new-password" />
        </div>
        <p v-if="pwdError" class="form-error">{{ pwdError }}</p>
        <div class="modal-actions">
          <button class="btn ghost" @click="showChangePwd = false">取消</button>
          <button class="btn primary" :disabled="pwdLoading" @click="handleChangePassword">
            {{ pwdLoading ? '提交中…' : '确认修改' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 设置播放密码弹窗 -->
    <div v-if="showUnlockConfirm && viewPermission?.realm_free" class="modal-mask" @click.self="showUnlockConfirm = false">
      <div class="modal">
        <h3 class="modal-title">确认解锁查看权限</h3>
        <p class="modal-desc">
          将花费 <strong>{{ viewPermission.unlock_points }}</strong> 积分（当前 {{ viewPermission.points_balance }}），
          有效期 {{ viewPermission.unlock_days > 0 ? viewPermission.unlock_days + ' 天' : '永久' }}。
          解锁后可查看服务器地址、Emby 账号与线路。
        </p>
        <div class="modal-actions">
          <button class="btn ghost" @click="showUnlockConfirm = false">取消</button>
          <button class="btn primary" :disabled="unlocking" @click="handleUnlockView">
            {{ unlocking ? '解锁中…' : '确认解锁' }}
          </button>
        </div>
      </div>
    </div>
    <div v-if="showSetPlayPwd" class="modal-mask" @click.self="showSetPlayPwd = false">
      <div class="modal">
        <h3 class="modal-title">设置 Emby 播放密码</h3>
        <p class="modal-desc">此密码用于在 Emby 客户端（Infuse、Forward 等）中登录</p>
        <div class="field">
          <label class="field-label">播放密码</label>
          <input v-model="playPwd" type="password" autocomplete="new-password" placeholder="至少 6 位" />
        </div>
        <div class="modal-actions">
          <button class="btn ghost" @click="showSetPlayPwd = false">取消</button>
          <button class="btn primary" :disabled="playPwdLoading || !playPwd" @click="handleSetPlayPassword">
            {{ playPwdLoading ? '提交中…' : '确认设置' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* ============================================================
   个人中心 — 最终版样式（2026-10-10）
   设计语言：暗房影院 · 放映机票根式 Hero · Spotify 三级表面
   约束：8pt 网格 / 数字 tabular-nums + nowrap / 琥珀三处原则 /
         零硬编码色值（全 --au-*）/ prefers-reduced-motion
   ============================================================ */

.profile-page {
  display: flex;
  flex-direction: column;
  gap: 1.125rem;
  /* 底部导航占位：用系统变量精确留白 */
  padding-bottom: 80px;
}

/* ==================== Hero 身份区 ==================== */
.hero {
  position: relative;
  overflow: hidden;
  border-radius: 20px;
  background: linear-gradient(180deg, var(--au-surface), var(--au-bg-soft));
  border: 1px solid var(--au-border);
  padding: 28px 24px 0;
}

/* ===== 我的页骨架屏 ===== */
.hero-skeleton {
  display: flex;
  gap: 16px;
  align-items: center;
  padding: 4px 0 20px;
}
.sk-avatar {
  width: 64px;
  height: 64px;
  border-radius: 50%;
  flex-shrink: 0;
}
.sk-hero-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.sk-hero-name {
  width: 40%;
  max-width: 160px;
  height: 24px;
}
.sk-hero-meta {
  width: 70%;
  height: 14px;
}
@media (prefers-reduced-motion: reduce) {
  .hero-skeleton .au-skeleton {
    animation: none;
  }
}

/* 氛围光：radial 琥珀晕开 + feTurbulence 颗粒，深色专享 */
.hero-ambiance {
  position: absolute;
  inset: 0;
  background:
    radial-gradient(ellipse 70% 90% at 15% 0%, rgba(232, 168, 74, 0.14), transparent 70%),
    url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='140' height='140' filter='url(%23n)' opacity='0.5'/%3E%3C/svg%3E");
}

html[data-theme="light"] .hero-ambiance {
  display: none;
}

.hero-top {
  display: flex;
  gap: 20px;
  align-items: center;
  position: relative;
  z-index: 1;
}

.hero-avatar {
  width: 72px;
  height: 72px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  background: var(--au-surface-2);
  border: 1px solid var(--au-border-strong);
  color: var(--au-text);
  font-family: var(--au-font-serif);
  font-size: 28px;
  font-weight: 500;
}

/* 会员光环：Opal 辉光选中态（深色底用辉光不用描边） */
.hero-avatar.is-vip {
  border-color: rgba(232, 168, 74, 0.55);
  box-shadow: 0 0 16px rgba(232, 168, 74, 0.28);
}

.hero-main {
  min-width: 0;
  flex: 1;
}

/* 微标签：11px 大写，字距 .12em */
.micro-label {
  margin: 0 0 6px;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: var(--au-text-4);
}

.hero-name {
  margin: 0;
  font-family: var(--au-font-serif);
  font-size: 28px;
  font-weight: 500;
  letter-spacing: 0.02em;
  color: var(--au-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.hero-sub {
  margin: 8px 0 0;
}

/* 票根徽章：琥珀三处原则之一（会员身份） */
.ticket {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 12px;
  border-radius: var(--au-r-full);
  border: 1px solid var(--au-primary-border);
  background: var(--au-primary-soft);
  color: var(--au-primary);
  font-size: 13px;
  font-weight: 600;
  white-space: nowrap;
}

.hero-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 10px;
}

.meta-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--au-text-3);
  white-space: nowrap;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}

.meta-dot {
  width: 3px;
  height: 3px;
  flex-shrink: 0;
  border-radius: 50%;
  background: var(--au-text-4);
}

.hero-stats {
  display: flex;
  position: relative;
  z-index: 1;
  margin-top: 24px;
  border-top: 1px solid var(--au-border);
}

.hero-stat {
  flex: 1;
  min-width: 0;
  padding: 18px 8px;
  text-align: center;
}

.hero-stat + .hero-stat {
  border-left: 1px solid var(--au-border);
}

.hero-stat strong {
  display: block;
  font-family: var(--au-font-serif);
  font-size: 22px;
  font-weight: 500;
  color: var(--au-text);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.hero-stat span {
  display: block;
  margin-top: 4px;
  font-size: 12px;
  color: var(--au-text-4);
  white-space: nowrap;
}

/* ==================== 网格 ==================== */
.p-grid {
  display: grid;
  /* 列宽一律 minmax(0, …)：裸写 1fr 等于 minmax(auto, 1fr)，轨道最小宽度 = 内容的
     min-content，而 Emby 账号卡里那行 nowrap 的等宽地址（https://emby.…）的 min-content
     就是它整行的宽度——整列被撑出屏幕，下面每张卡都跟着出界 */
  grid-template-columns: minmax(0, 1.3fr) minmax(0, 1fr);
  gap: 1.125rem;
  align-items: start;
}

.p-side {
  display: flex;
  flex-direction: column;
  gap: 1.125rem;
  min-width: 0;
}

/* ==================== 卡片 ==================== */
.pane {
  min-width: 0;
  padding: 1.25rem;
  background: var(--au-surface);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-lg);
  animation: pane-in 0.45s var(--au-ease) both;
}

/* 右列卡片 stagger 入场（60fps.design：50-100ms 间隔） */
.p-side .pane:nth-child(2) { animation-delay: 0.06s; }
.p-side .pane:nth-child(3) { animation-delay: 0.12s; }
.p-side .pane:nth-child(4) { animation-delay: 0.18s; }
.p-side .pane:nth-child(5) { animation-delay: 0.24s; }

@keyframes pane-in {
  from { opacity: 0; transform: translateY(12px); }
  to { opacity: 1; transform: none; }
}

.pane-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  margin-bottom: 1rem;
}

.pane-title {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  margin: 0;
  font-family: var(--au-font-serif);
  font-size: 17px;
  font-weight: 500;
  letter-spacing: 0.02em;
  color: var(--au-text);
}

.pane-title svg { color: var(--au-primary); }

.pane-empty {
  margin: 0;
  font-size: 0.8125rem;
  line-height: 1.6;
  color: var(--au-text-3);
}

.pane-tip {
  margin: 0.875rem 0 0;
  padding-top: 0.875rem;
  border-top: 1px solid var(--au-border);
  font-size: 0.8125rem;
  line-height: 1.5;
  color: var(--au-text-3);
}

/* ==================== 徽章 ==================== */
.badge {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  padding: 0.0625rem 0.5rem;
  border-radius: var(--au-r-full);
  font-size: 0.8125rem;
  white-space: nowrap;
  background: var(--au-surface-2);
  color: var(--au-text-3);
}

.badge.ok { background: var(--au-success-soft); color: var(--au-success); }
.badge.idle { background: var(--au-surface-2); color: var(--au-text-3); }
.badge.off { background: var(--au-danger-soft); color: var(--au-danger); }
/* 临期：与后台「到期前提醒」同色系 */
.badge.warn { background: var(--au-warning-soft); color: var(--au-warning); }
/* 公益服：免费开放不是"未开通"，用站点主色单独区分 */
.badge.free { background: var(--au-primary-soft); color: var(--au-primary); }

.sub-free { display: flex; flex-direction: column; gap: 0.375rem; align-items: flex-start; }

.count-chip {
  margin-left: 0.125rem;
  padding: 0.0625rem 0.4375rem;
  border-radius: var(--au-r-full);
  background: var(--au-surface-2);
  font-size: 0.8125rem;
  font-weight: 500;
  color: var(--au-text-3);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

/* ==================== 按钮 ==================== */
/* 按压缩放：Airbnb 触觉回弹 cubic-bezier(0.34,1.56,0.64,1)，120ms */
.icon-btn,
.copy-btn,
.text-btn,
.btn,
.scheme-btn,
.list-item {
  transition: transform 0.12s cubic-bezier(0.34, 1.56, 0.64, 1);
}

.icon-btn:active,
.copy-btn:active,
.text-btn:active:not(:disabled),
.btn:active:not(:disabled),
.scheme-btn:active,
.list-item:active {
  transform: scale(0.97);
}

.icon-btn {
  width: 32px;
  height: 32px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--au-surface-2);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-sm);
  color: var(--au-text-2);
  cursor: pointer;
}

.icon-btn:hover {
  color: var(--au-text);
  border-color: var(--au-border-strong);
}

.spinning { animation: spin 0.9s linear infinite; }

@keyframes spin {
  to { transform: rotate(360deg); }
}

.copy-btn {
  flex-shrink: 0;
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  border: none;
  border-radius: var(--au-r-sm);
  color: var(--au-text-3);
  cursor: pointer;
}

.copy-btn:hover {
  background: var(--au-surface-2);
  color: var(--au-text);
}

.copy-btn .ok { color: var(--au-success); }

.text-btn {
  flex-shrink: 0;
  padding: 0.25rem 0.5rem;
  background: transparent;
  border: none;
  border-radius: var(--au-r-sm);
  color: var(--au-primary);
  font-size: 0.8125rem;
  white-space: nowrap;
  cursor: pointer;
}

.text-btn:hover { background: var(--au-primary-soft); }

.text-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.text-btn.danger { color: var(--au-text-3); }
.text-btn.danger:hover { background: var(--au-danger-soft); color: var(--au-danger); }
.text-btn.danger.confirming { color: var(--au-danger); font-weight: 600; }

.btn {
  height: 38px;
  padding: 0 1rem;
  border: none;
  border-radius: var(--au-r-md);
  font-size: 0.875rem;
  font-weight: 500;
  white-space: nowrap;
  cursor: pointer;
}

.btn.ghost {
  background: var(--au-surface-2);
  color: var(--au-text-2);
}

.btn.ghost:hover { background: var(--au-surface-3); }

/* 主按钮：琥珀三处原则之一 */
.btn.primary {
  background: var(--au-primary);
  color: var(--au-on-primary);
  font-weight: 600;
}

.btn.primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* ==================== Emby 账号信息行 ==================== */
.account-error {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 0.875rem 0;
}

.account-error-text {
  margin: 0;
  font-size: 0.8125rem;
  color: var(--au-danger, #e5484d);
}

.rows {
  display: flex;
  flex-direction: column;
  gap: 0.625rem;
}

/* 呼吸式加载（Wabi Soothing Loading）：opacity 0.45↔0.7，2s 周期 */
.rows.loading {
  pointer-events: none;
  animation: breathe 2s ease-in-out infinite;
}

@keyframes breathe {
  0%, 100% { opacity: 0.45; }
  50% { opacity: 0.7; }
}

.row {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 12px 14px;
  background: var(--au-input-bg);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-md);
}

.row-label {
  flex-shrink: 0;
  font-size: 13px;
  color: var(--au-text-3);
  white-space: nowrap;
}

.row-value {
  flex: 1;
  min-width: 0;
  margin-left: auto;
  font-size: 13px;
  color: var(--au-text);
  text-align: right;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}

/* 公益规则这类长文本要换行，不能像地址那样截断 */
.row-value.wrap { overflow: visible; white-space: normal; line-height: 1.6; text-align: left; }

.mono {
  font-family: ui-monospace, 'SF Mono', Menlo, Consolas, monospace;
}

/* ==================== 解锁卡 ==================== */
.unlock-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.625rem;
  padding: 24px 16px;
  border: 1px dashed var(--au-primary-border);
  border-radius: var(--au-r-lg);
  background: var(--au-surface-2);
  text-align: center;
}

.unlock-title {
  margin: 0;
  font-family: var(--au-font-serif);
  font-size: 17px;
  font-weight: 500;
  color: var(--au-text);
}

.unlock-desc {
  margin: 0;
  font-size: 0.8125rem;
  line-height: 1.6;
  color: var(--au-text-3);
}

.unlock-desc strong {
  color: var(--au-text);
  font-variant-numeric: tabular-nums;
}

.unlock-hint {
  margin: 0;
  font-size: 0.8125rem;
  color: var(--au-text-4);
}

/* ==================== 多服 / 一键导入 ==================== */
.sub-block {
  margin-top: 1.125rem;
}

.block-label {
  display: block;
  margin-bottom: 0.5rem;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: var(--au-text-4);
}

.realm-list {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.realm-item {
  padding: 12px 14px;
  background: var(--au-surface-2);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-sm);
}

.realm-head {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.8125rem;
  color: var(--au-text-2);
}

.realm-head strong { color: var(--au-text); }

.realm-url {
  display: flex;
  align-items: center;
  gap: 0.375rem;
  margin-top: 0.25rem;
  font-size: 0.8125rem;
  color: var(--au-text-3);
  word-break: break-all;
}

.realm-url .mono { flex: 1; }

.realm-url.muted { color: var(--au-text-4); font-style: normal; }

.scheme-btns {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
}

.scheme-btn {
  display: inline-flex;
  align-items: center;
  gap: 0.375rem;
  height: 36px;
  padding: 0 1rem;
  background: var(--au-surface-2);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-full);
  color: var(--au-text-2);
  font-size: 0.8125rem;
  font-weight: 500;
  white-space: nowrap;
  cursor: pointer;
}

.scheme-btn:hover {
  background: var(--au-primary-soft);
  border-color: var(--au-primary-border);
  color: var(--au-primary);
}

/* ==================== 我的订阅 ==================== */
.sub-active {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  padding: 0.875rem 1rem;
  background: var(--au-primary-soft);
  border: 1px solid var(--au-primary-border);
  border-radius: var(--au-r-md);
}

.sub-plan {
  font-weight: 600;
  color: var(--au-text);
  font-variant-numeric: tabular-nums;
}

.sub-end {
  margin-top: 0.125rem;
  font-size: 0.8125rem;
  color: var(--au-text-3);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

/* 临期状态：整块底色换成警示色，避免"还剩 3 天"淹没在常规配色里 */
.sub-active.warn {
  background: var(--au-warning-soft);
  border-color: var(--au-warning-soft);
}

.sub-warn {
  display: flex;
  align-items: center;
  gap: 0.3125rem;
  margin: 0.375rem 0 0;
  font-size: 0.8125rem;
  color: var(--au-warning);
}

.sub-history {
  list-style: none;
  margin: 0.75rem 0 0;
  padding: 0;
}

.sub-history-item {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.4375rem 0;
  font-size: 0.8125rem;
  color: var(--au-text-2);
}

.sub-history-item + .sub-history-item { border-top: 1px solid var(--au-border); }

.sub-history-date {
  flex: 1;
  color: var(--au-text-3);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.sub-realm {
  font-style: normal;
  font-size: 0.8125rem;
  color: var(--au-text-3);
}

/* ==================== 我的设备 ==================== */
.dev-row {
  display: flex;
  align-items: center;
  gap: 0.625rem;
  padding: 12px 0;
}

.dev-row + .dev-row { border-top: 1px solid var(--au-border); }

.dev-main {
  display: flex;
  flex-direction: column;
  gap: 0.125rem;
  min-width: 0;
  flex: 1;
}

.dev-name {
  font-size: 0.875rem;
  font-weight: 500;
  color: var(--au-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.dev-meta {
  font-size: 0.8125rem;
  color: var(--au-text-4);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}

/* ==================== 安全设置 ==================== */
.list {
  display: flex;
  flex-direction: column;
}

.list-item {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  width: 100%;
  min-height: 52px;
  padding: 0.8125rem 0.25rem;
  background: transparent;
  border: none;
  border-top: 1px solid var(--au-border);
  color: var(--au-text-2);
  font-size: 0.9375rem;
  text-align: left;
  text-decoration: none;
  cursor: pointer;
}

.list-item:first-child { border-top: none; }

.list-item:hover { color: var(--au-text); }

.list-item.danger { color: var(--au-danger); }

.list-icon { color: var(--au-text-3); flex-shrink: 0; }

.list-item.danger .list-icon { color: var(--au-danger); opacity: 0.8; }

.list-text { flex: 1; }

.list-arrow { color: var(--au-text-4); flex-shrink: 0; }

/* ==================== 播放设置 ==================== */
.line-only {
  display: flex;
  align-items: center;
  gap: 0.4375rem;
  margin: 0;
  padding: 0.625rem 0.75rem;
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-md);
  background: var(--au-surface-2);
  color: var(--au-text-2);
  font-size: 0.8125rem;
  line-height: 1.5;
}

.line-only svg { flex-shrink: 0; color: var(--au-primary); }

/* ==================== 弹窗 ==================== */
.modal-mask {
  position: fixed;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 1.5rem;
  background: var(--au-scrim);
  z-index: 100;
  animation: fade-in 0.2s var(--au-ease);
}

@keyframes fade-in {
  from { opacity: 0; }
  to { opacity: 1; }
}

.modal {
  width: 100%;
  max-width: 360px;
  padding: 1.5rem;
  background: var(--au-bg-soft);
  border: 1px solid var(--au-border-strong);
  border-radius: var(--au-r-lg);
  animation: modal-up 0.25s cubic-bezier(0.34, 1.3, 0.64, 1);
}

@keyframes modal-up {
  from { opacity: 0; transform: translateY(16px) scale(0.98); }
  to { opacity: 1; transform: none; }
}

.modal-title {
  margin: 0 0 0.375rem;
  font-family: var(--au-font-serif);
  font-size: 19px;
  font-weight: 500;
  color: var(--au-text);
}

.modal-desc {
  margin: 0 0 1rem;
  font-size: 0.8125rem;
  line-height: 1.5;
  color: var(--au-text-3);
}

.field { margin-bottom: 0.875rem; }

.field-label {
  display: block;
  margin-bottom: 0.375rem;
  font-size: 0.8125rem;
  color: var(--au-text-3);
}

.field input {
  width: 100%;
  height: 42px;
  padding: 0 0.75rem;
  background: var(--au-bg);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-sm);
  color: var(--au-text);
  font-size: 0.875rem;
  outline: none;
  transition: border-color var(--au-fast) var(--au-ease), box-shadow var(--au-fast) var(--au-ease);
  box-sizing: border-box;
}

.field input:focus {
  border-color: var(--au-border-focus);
  box-shadow: 0 0 0 3px var(--au-primary-soft);
}

.form-error {
  margin: 0 0 0.75rem;
  padding: 0.5rem 0.75rem;
  background: var(--au-danger-soft);
  border: 1px solid var(--au-danger-border);
  border-radius: var(--au-r-sm);
  color: var(--au-danger);
  font-size: 0.8125rem;
}

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 0.625rem;
  margin-top: 1.125rem;
}

/* ==================== 响应式 ==================== */
@media (max-width: 900px) {
  .p-grid { grid-template-columns: minmax(0, 1fr); }
}

@media (max-width: 640px) {
  .hero {
    padding: 24px 20px 0;
    border-radius: 16px;
  }

  .hero-top { gap: 16px; }

  .hero-avatar {
    width: 60px;
    height: 60px;
    font-size: 24px;
  }

  .hero-name { font-size: 24px; }

  .hero-stat strong { font-size: 19px; }

  .pane { padding: 1rem; }

  /* 拇指区：列表项最小 52px 高已在 .list-item 保证 */
  .scheme-btn { height: 40px; }
  .btn { height: 44px; }
}

/* ==================== 动效偏好 ==================== */
@media (prefers-reduced-motion: reduce) {
  .pane { animation: none; }
  .rows.loading { animation: none; opacity: 0.55; }
  .spinning { animation-duration: 2.4s; }
  .modal-mask, .modal { animation: none; }
  .icon-btn:active,
  .copy-btn:active,
  .text-btn:active:not(:disabled),
  .btn:active:not(:disabled),
  .scheme-btn:active,
  .list-item:active {
    transform: none;
  }
}
</style>