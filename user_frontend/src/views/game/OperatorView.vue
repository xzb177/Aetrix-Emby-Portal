<template>
  <div class="v4-scope">
    <div class="v4-page">
      <!-- 日志头 -->
      <header class="v4-loghead">
        <span class="v4-stamp">营地 · 接线处</span>
        <h1><span aria-hidden="true">🔌</span> 接线员</h1>
        <p>买完会员来这儿接线 —— 拿上账号密码，去播放器里连上就能看。</p>
      </header>

      <!-- 加载中 -->
      <div v-if="loading && !account" class="v4-sec">
        <div class="v4-skeleton" style="height:180px" role="status" aria-label="正在加载接线信息"></div>
      </div>

      <!-- 加载失败 -->
      <div v-else-if="accountError && !account" class="v4-sec">
        <div class="v4-err">
          接线员没接上：{{ accountError }}<br>
          <button class="v4-retry" @click="refreshAccount()">重试</button>
        </div>
      </div>

      <template v-else>
        <!-- 公益服说明 -->
        <div v-if="isFreeRealm" class="v4-sec">
          <div class="v4-sec-title"><span aria-hidden="true">📻</span> 接入方式</div>
          <div class="free-note">
            <span class="badge free">公益服 · 免费开放</span>
            <p>{{ realmNote }}</p>
          </div>
        </div>

        <!-- 有查看权限：账号密码卡 -->
        <template v-if="viewGranted">
          <section class="v4-sec">
            <div class="v4-sec-title"><span aria-hidden="true">🔑</span> 你的接线账号</div>
            <div class="v4-sec-sub">拿着这三样，去播放器里登录</div>

            <div class="op-rows" :class="{ loading: loading && !accountReloading }">
              <template v-if="accountReloading">
                <div class="v4-skeleton" style="height:54px" role="status" aria-label="正在刷新账号信息"></div>
                <div class="v4-skeleton" style="height:54px" aria-hidden="true"></div>
                <div class="v4-skeleton" style="height:54px" aria-hidden="true"></div>
              </template>
              <template v-else>
              <div class="op-row">
                <span class="op-label">服务器</span>
                <span class="op-value mono">{{ serverUrl }}</span>
                <button class="copy-btn" title="复制服务器地址" aria-label="复制服务器地址" @click="copyText(serverUrl, 'server')">
                  <Check v-if="copiedField === 'server'" :size="14" class="ok" />
                  <Copy v-else :size="14" />
                </button>
              </div>
              <div class="op-row">
                <span class="op-label">用户名</span>
                <span class="op-value mono">{{ embyUsername }}</span>
                <button class="copy-btn" title="复制用户名" aria-label="复制用户名" @click="copyText(embyUsername, 'user')">
                  <Check v-if="copiedField === 'user'" :size="14" class="ok" />
                  <Copy v-else :size="14" />
                </button>
              </div>
              <div class="op-row">
                <span class="op-label">播放密码</span>
                <span class="op-value mono">
                  <template v-if="hasPlayPassword">{{ showPlayPassword && playPwdLoaded ? playPasswordText : '••••••••' }}</template>
                  <template v-else>未设置</template>
                </span>
                <button
                  v-if="playPwdLoaded"
                  class="copy-btn"
                  :title="showPlayPassword ? '隐藏' : '查看'"
                  :aria-label="showPlayPassword ? '隐藏' : '查看'"
                  @click="showPlayPassword = !showPlayPassword"
                >
                  <Eye v-if="showPlayPassword" :size="14" />
                  <EyeOff v-else :size="14" />
                </button>
                <button class="text-btn" @click="openSetPlayPwd">
                  {{ hasPlayPassword ? '修改' : '设置' }}
                </button>
              </div>
              </template>
            </div>

            <p class="op-tip">
              播放密码用于 Emby 客户端登录，和这个网站的登录密码是两回事，互不影响。
            </p>
          </section>

          <!-- 多服 -->
          <section v-if="realmCards.length > 1" class="v4-sec">
            <div class="v4-sec-title"><span aria-hidden="true">🗺️</span> 我的服</div>
            <div class="v4-sec-sub">每个服有自己的地址和会员</div>
            <div class="realm-list">
              <div v-for="r in realmCards" :key="r.id" class="realm-item">
                <div class="realm-head">
                  <strong>{{ r.name }}</strong>
                  <span v-if="r.is_free" class="badge free">公益服</span>
                  <span v-else-if="r.subscribed" class="badge ok">{{ daysLeftText(r.end_date) }}</span>
                  <span v-else class="badge off">未开通</span>
                </div>
                <div v-if="r.view_granted" class="realm-url">
                  <span class="mono">{{ r.base_url || '管理员还没填这个服的地址' }}</span>
                  <button
                    v-if="r.base_url"
                    class="copy-btn"
                    :title="'复制服务器地址：' + r.name"
                    :aria-label="'复制服务器地址：' + r.name"
                    @click="copyText(r.base_url, `realm-${r.id}`)"
                  >
                    <Check v-if="copiedField === `realm-${r.id}`" :size="13" class="ok" />
                    <Copy v-else :size="13" />
                  </button>
                </div>
                <div v-else class="realm-url muted">
                  <span>{{ r.is_free ? '花能量解锁后可见' : '开通会员后可见' }}</span>
                </div>
              </div>
            </div>
          </section>

          <!-- 一键导入 -->
          <section v-if="hasSchemes" class="v4-sec">
            <div class="v4-sec-title"><span aria-hidden="true">📲</span> 一键导入到客户端</div>
            <div class="v4-sec-sub">点一下，直接跳到播放器里填好</div>
            <div class="scheme-btns">
              <button
                v-for="(url, name) in importSchemes"
                :key="name"
                class="scheme-btn"
                @click="openScheme(url, name)"
              >
                {{ name }}
              </button>
            </div>
            <p class="op-tip">{{ importTip }}</p>
          </section>

          <!-- 正在播放：旧 PlaybackSessions 一比一恢复（#783 删页面时丢了入口） -->
          <section class="v4-sec" aria-label="正在播放">
            <div class="v4-sec-title">
              <span aria-hidden="true">📺</span> 正在播放
              <button
                class="refresh-btn"
                :class="{ 'is-loading': sessionsLoading }"
                :disabled="sessionsLoading"
                :aria-label="sessionsLoading ? '正在刷新播放列表' : '刷新正在播放'"
                @click="loadSessions"
              >
                {{ sessionsLoading ? '刷新中' : '⟳' }}
              </button>
            </div>
            <div class="v4-sec-sub">这个账号正在哪台设备上播放，一键远程停掉</div>

            <div v-if="sessionsLoading" class="op-session-list" role="status" aria-label="正在加载播放列表">
              <div class="v4-skeleton" style="height:64px"></div>
              <div class="v4-skeleton" style="height:64px"></div>
            </div>

            <div v-else-if="sessionsError" class="op-session-list">
              <div class="v4-err">
                正在播放没加载出来<br>
                <button class="v4-retry" @click="loadSessions">重试</button>
              </div>
            </div>

            <div v-else-if="sessions.length === 0" class="op-empty">
              <MonitorSmartphone :size="26" aria-hidden="true" />
              <p>现在没有正在播放的</p>
              <p class="op-empty-sub">在任意设备上开始播放后，会在这里显示并可远程结束</p>
            </div>

            <div v-else class="op-session-list">
              <div v-for="s in sessions" :key="s.session_key" class="op-session-row">
                <div class="op-session-icon">
                  <MonitorSmartphone :size="17" aria-hidden="true" />
                </div>

                <div class="op-session-main">
                  <div class="op-session-title">
                    <span class="op-session-name" :title="s.item">{{ s.item }}</span>
                    <span class="badge type">{{ typeLabel(s.item_type) }}</span>
                    <span v-if="s.is_paused" class="badge paused">已暂停</span>
                  </div>
                  <div class="op-session-meta">
                    <span>{{ s.device || '未知设备' }}</span>
                    <span v-if="s.client" class="meta-sep">·</span>
                    <span v-if="s.client">{{ s.client }}</span>
                    <span v-if="s.play_method" class="meta-sep">·</span>
                    <span v-if="s.play_method" class="method">{{ s.play_method === 'Transcode' ? '转码' : '直连' }}</span>
                    <span v-if="s.remote_addr" class="meta-sep">·</span>
                    <span v-if="s.remote_addr" class="addr"><Wifi :size="11" aria-hidden="true" />{{ s.remote_addr }}</span>
                  </div>
                  <div class="op-session-progress">
                    <div class="bar" role="progressbar" :aria-valuenow="Math.round(s.progress)" aria-valuemin="0" aria-valuemax="100" :aria-label="'播放进度 ' + Math.round(s.progress) + '%'"><div class="bar-fill" :style="{ transform: 'scaleX(' + Math.min(100, Math.max(0, s.progress)) / 100 + ')' }" /></div>
                    <span class="bar-text">{{ Math.round(s.progress) }}%<template v-if="s.updated_at"> · {{ fmtTime(s.updated_at) }}</template></span>
                  </div>
                </div>

                <button
                  class="op-stop-btn"
                  :disabled="stopping === s.session_key"
                  :aria-label="'结束播放：' + (s.item || s.session_key)"
                  @click="confirmStopSession(s)"
                >
                  <CircleStop :size="13" aria-hidden="true" />
                  {{ stopping === s.session_key ? '结束中' : '结束' }}
                </button>
              </div>
            </div>
          </section>

          <!-- 怎么连：小白指引 -->
          <section class="v4-sec">
            <div class="v4-sec-title"><span aria-hidden="true">🧭</span> 怎么连上？</div>
            <div class="guide-steps">
              <div class="guide-step">
                <span class="step-num">1</span>
                <div>
                  <b>复制上面三样</b>
                  <p>服务器地址、用户名、播放密码，点右边按钮复制。</p>
                </div>
              </div>
              <div class="guide-step">
                <span class="step-num">2</span>
                <div>
                  <b>打开播放器</b>
                  <p>比如 Infuse、Forward，选"添加 Emby 服务器"。</p>
                </div>
              </div>
              <div class="guide-step">
                <span class="step-num">3</span>
                <div>
                  <b>粘贴登录</b>
                  <p>把三样粘贴进去，登录就能看了。嫌麻烦就用上面的一键导入。</p>
                </div>
              </div>
            </div>
          </section>
        </template>

        <!-- 没查看权限：解锁入口 -->
        <div v-else-if="viewPermission" class="v4-sec">
          <div class="v4-sec-title"><span aria-hidden="true">🔒</span> 接线需要解锁</div>
          <template v-if="viewPermission.realm_free">
            <p class="unlock-desc">
              花 <strong>{{ viewPermission.unlock_points }}</strong> 能量解锁查看权限，
              有效期 {{ viewPermission.unlock_days > 0 ? viewPermission.unlock_days + ' 天' : '永久' }}。
              你当前有 <strong>{{ viewPermission.points_balance }}</strong> 能量。
            </p>
            <button
              class="op-btn primary"
              :disabled="unlocking || viewPermission.points_balance < viewPermission.unlock_points"
              @click="openUnlockConfirm"
            >
              {{ unlocking ? '解锁中…' : (viewPermission.points_balance < viewPermission.unlock_points ? '能量不足' : `花 ${viewPermission.unlock_points} 能量解锁`) }}
            </button>
            <p class="op-tip">能量可通过每日报到、邀请好友等方式获得。</p>
          </template>
          <template v-else>
            <p class="unlock-desc">服务器地址和账号只向会员开放，开通后回来这儿接线。</p>
            <button class="op-btn primary" @click="router.push('/shop')">去开通会员</button>
          </template>
        </div>

        <div v-else class="v4-sec">
          <div class="v4-err">
            接线员没接上，再试一次<br>
            <button class="v4-retry" @click="refreshAccount()">重试</button>
          </div>
        </div>
      </template>
    </div>

    <!-- 设置播放密码弹窗 -->
    <Teleport to="body">
      <div v-if="showSetPlayPwd" class="modal-mask" @click.self="closeSetPlayPwd()" @keydown="onModalKeydown">
        <div class="modal-card" role="dialog" aria-modal="true" aria-label="设置播放密码">
          <h3 class="modal-title">设置 Emby 播放密码</h3>
          <p class="modal-desc">这个密码用在 Emby 客户端（Infuse、Forward 等）里登录，至少 6 位。</p>
          <input
            v-model="playPwd"
            type="password"
            class="modal-input"
            placeholder="输入新密码（至少 6 位）"
            aria-label="设置播放密码"
            autocomplete="new-password"
          />
          <p v-if="pwdError" class="modal-error" role="alert">{{ pwdError }}</p>
          <div class="modal-actions">
            <button class="op-btn ghost" @click="closeSetPlayPwd()">取消</button>
            <button class="op-btn primary" :disabled="settingPwd" @click="confirmSetPassword">
              {{ settingPwd ? '设置中' : '确认' }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- 解锁确认弹窗 -->
    <Teleport to="body">
      <div v-if="showUnlockConfirm" class="modal-mask" @click.self="closeUnlockConfirm()" @keydown="onModalKeydown">
        <div class="modal-card" role="dialog" aria-modal="true" aria-label="确认解锁">
          <h3 class="modal-title">确认解锁？</h3>
          <p class="modal-desc">
            将花费 <strong>{{ viewPermission?.unlock_points }}</strong> 能量解锁查看权限。
          </p>
          <div class="modal-actions">
            <button class="op-btn ghost" @click="closeUnlockConfirm()">取消</button>
            <button class="op-btn primary" :disabled="unlocking" @click="confirmUnlock">
              {{ unlocking ? '解锁中' : '确认' }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- 结束播放确认弹窗（二次确认，防手滑） -->
    <Teleport to="body">
      <div v-if="sessionToStop" class="modal-mask" @click.self="closeStopSession()" @keydown="onModalKeydown">
        <div class="modal-card" role="dialog" aria-modal="true" aria-label="确认结束播放">
          <h3 class="modal-title">结束这次播放？</h3>
          <p class="modal-desc">
            将在「<strong>{{ sessionToStop.device || '未知设备' }}</strong>」上停掉
            「<strong>{{ sessionToStop.item }}</strong>」的播放。
          </p>
          <p v-if="stopError" class="modal-error" role="alert">{{ stopError }}</p>
          <div class="modal-actions">
            <button class="op-btn ghost" :disabled="!!stopping" @click="closeStopSession()">取消</button>
            <button class="op-btn primary" :disabled="!!stopping" @click="doStopSession">
              {{ stopping ? '结束中' : (stopError ? '重试' : '确认结束') }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>

  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { Check, CircleStop, Copy, Eye, EyeOff, MonitorSmartphone, Wifi } from 'lucide-vue-next'
import { useToast } from '@/composables/useToast'
import { useClipboard } from '@/composables/useClipboard'
import { humanizeError } from '@/composables/usePanelApi'
import { toUserMessage } from '@/utils/toUserMessage'
import {
  embyApi,
  type AccountCard, type AccountRealmCard, type MyPlaybackSession, type ViewPermission,
} from '@/api'
import { useUserStore } from '@/stores/user'

const router = useRouter()
const toast = useToast()
// R41：复制走全站唯一切口（照抄 CompanionView 同一模式）
const { copy } = useClipboard()
const userStore = useUserStore()

const loading = ref(true)
const account = ref<AccountCard | null>(null)
const accountError = ref('')
/** R50：空占位符（与 copyText 的空值判定共用一处，改一处不漏另一处） */
const EMPTY_DASH = '—'

/** R50：后端账号卡实际下发、但 AccountCard 类型尚未收录的字段（渐进补齐，避免满屏 as any） */
interface AccountCardExtra {
  /** 兼容字段：部分部署 server_url 与 base_url 并存 */
  server_url?: string
  /** 公益服标记（顶层兼容字段） */
  realm_free?: boolean
  /** 公益服说明（顶层兼容字段） */
  realm_note?: string
}
/** R50：view_permission 里后端可能多带的字段 */
interface ViewPermissionExtra {
  realm_id?: number | null
  realm_note?: string
}
/** R52-CI：AccountCardExtra 是弱类型（全可选字段），直接把 AccountCard 往它上面靠
    会触发"无公共属性"报错；与 AccountCard 取交集才是卡的完整形态 */
type AccountCardFull = AccountCard & AccountCardExtra
const accountExtra = computed((): AccountCardFull => (account.value ?? {}) as AccountCardFull)
/** R50：账号已就绪时的刷新（重试按钮场景）——op-rows 显示骨架而不是整块变暗 */
const accountReloading = computed(() => loading.value && !!account.value)
// 查看权限：后端账号卡自带 view_permission（没权限时 200 返回卡，granted=false，只给解锁信息不下发地址）
// 和 ProfileView 同一口径：从卡里派生，不单独调接口（后端没有 /view-permission 这类端点）
const viewPermission = computed(() => (account.value?.view_permission || null) as ((ViewPermission & ViewPermissionExtra) | null))
const unlocking = ref(false)
const showUnlockConfirm = ref(false)

const copiedField = ref('')
const showPlayPassword = ref(false)
const playPasswordText = ref('')
/** R50：播放密码明文是否懒加载成功（失败时模板继续显示 ••••••••） */
const playPwdLoaded = ref(false)
const showSetPlayPwd = ref(false)
const playPwd = ref('')
const pwdError = ref('')
const settingPwd = ref(false)
/** R53：一键导入提示文案随密码状态走（三态） */
const importTip = computed(() => {
  if (playPwdLoaded.value) return '点一下直接导入，密码一起带过去，只用在这次跳转里，不会存'
  if (hasPlayPassword.value) return '密码已设置，这次没法自动带过去，导入后在播放器里输一次就行'
  return '还没设播放密码，导入后在播放器里也能补'
})

/** 是否有查看权限：以卡里 view_permission.granted 为准（和 ProfileView 同一口径） */
const viewGranted = computed(() => !!viewPermission.value?.granted)
const hasPlayPassword = computed(() => !!account.value?.has_password)

const serverUrl = computed(() => accountExtra.value.server_url || account.value?.base_url || EMPTY_DASH)
const embyUsername = computed(() => account.value?.emby_username || userStore.user?.username || EMPTY_DASH)
const isFreeRealm = computed(() => !!accountExtra.value.realm_free || !!viewPermission.value?.realm_free)
const realmNote = computed(() => accountExtra.value.realm_note || viewPermission.value?.realm_note || '')
const realmCards = computed((): AccountRealmCard[] => {
  const r = account.value?.realms
  return Array.isArray(r) ? r : []
})
const importSchemes = computed((): Record<string, string> => {
  const s = account.value?.import_schemes
  return s && typeof s === 'object' ? s : {}
})
const hasSchemes = computed(() => Object.keys(importSchemes.value).length > 0)

async function refreshAccount(quiet = false): Promise<boolean> {
  if (!quiet) loading.value = true
  accountError.value = ''
  try {
    const res = await embyApi.getAccountCard()
    account.value = res as AccountCard
    return true
  } catch (e: any) {
    // 没权限时后端直接 200 返回带 view_permission 的卡（granted=false），不会 403；
    // 这里只处理真正的加载失败（网络/鉴权），和 ProfileView 同一口径
    accountError.value = humanizeError(e)
    return false
  } finally {
    loading.value = false
  }
}

/** R41：裸调 navigator.clipboard?.writeText 在 HTTP 自建部署下是 undefined，
 *  点复制毫无反应还不告诉用户 —— 改用 useClipboard（Clipboard API + execCommand 兜底，
 *  成功失败都给人话反馈；照抄 CompanionView 同一切口） */
let copyTimer: number | undefined
async function copyText(text: string, field: string) {
  if (!text || text === EMPTY_DASH) {
    toast.info('没有可复制的内容')
    return
  }
  const label = field === 'server' ? '服务器地址' : field === 'user' ? '用户名' : '地址'
  const ok = await copy(text, label)
  if (!ok) return
  copiedField.value = field
  window.clearTimeout(copyTimer)
  copyTimer = window.setTimeout(() => { if (copiedField.value === field) copiedField.value = '' }, 1800)
}

async function confirmSetPassword() {
  pwdError.value = ''
  if (!playPwd.value || playPwd.value.length < 6) {
    pwdError.value = '密码至少 6 位'
    return
  }
  settingPwd.value = true
  try {
    await embyApi.setPassword(playPwd.value)
    toast.success('播放密码已设置')
    // R53：本会话内记住明文（后端 bcrypt 存的、取不回来）——密码行查看按钮与一键导入都用它
    playPasswordText.value = playPwd.value
    playPwdLoaded.value = true
    playPwd.value = ''
    closeSetPlayPwd()
    showPlayPassword.value = false
    await refreshAccount(true)
  } catch (e) {
    pwdError.value = humanizeError(e)
  } finally {
    settingPwd.value = false
  }
}

async function confirmUnlock() {
  unlocking.value = true
  try {
    const realmId = account.value?.realm_id ?? viewPermission.value?.realm_id ?? null
    // R50：realmId 取不到时不带该字段，不发 realm_id: null
    if (realmId != null) await embyApi.unlockView(realmId)
    else await embyApi.unlockView()
    closeUnlockConfirm()
    // R77-1：先刷新再报喜——刷新失败时页面还停在未解锁态，只报「解锁成功」
    // 会让用户以为解锁没生效；失败时明确告知解锁已成功、重进本页可见
    const refreshed = await refreshAccount()
    if (refreshed) toast.success('解锁成功')
    else toast.error('解锁成功，但页面刷新失败，重新进入本页就能看到账号')
  } catch (e) {
    toast.error(humanizeError(e))
  } finally {
    unlocking.value = false
  }
}

async function openScheme(url: string, appName = '播放器') {
  // R50：空地址直接提示，不跳转（空串 location.href 会刷新当前页）
  if (!url) {
    toast.info('播放地址无效')
    return
  }
  let finalUrl = url
  // R53：一键导入只用本会话已知密码（hasPlayPassword 但本会话未知时带不过去——明文取不回）
  if (playPwdLoaded.value) {
    try {
      const u = new URL(url)
      // 常见 scheme 参数名兼容：password / pwd
      if (!u.searchParams.has('password') && !u.searchParams.has('pwd')) {
        u.searchParams.set('password', playPasswordText.value)
      }
      finalUrl = u.toString()
    } catch {
      finalUrl = url
    }
  }
  // R41：没装 App 时 iOS 跳转是死路 —— 跳转前先说一声，提醒没装的去应用商店
  toast.info(`正在打开${appName}，没装的话请先去应用商店安装`)
  // R50：给 toast 一点展示时间再跳转，否则提示来不及被看到
  await nextTick()
  await new Promise((resolve) => setTimeout(resolve, 600))
  window.location.href = finalUrl
}

function daysLeft(endDate?: string | null): number | null {
  if (!endDate) return 0
  const t = new Date(endDate).getTime()
  // R50：非法日期返回 null（模板显示"—"），不再渲染 NaN
  if (!isFinite(t)) return null
  return Math.max(0, Math.ceil((t - Date.now()) / 86400000))
}

/** R50：会员剩余天数文案（daysLeft 只调一次；null → "—" 不显示"会员已到期"） */
function daysLeftText(endDate?: string | null): string {
  const d = daysLeft(endDate)
  if (d === null) return EMPTY_DASH
  return d > 0 ? `会员剩 ${d} 天` : '会员已到期'
}

// ===== 正在播放（旧 PlaybackSessions.vue 一比一恢复，放接线员页） =====
// 数据源：GET /api/user/emby/sessions；结束：DELETE /api/user/emby/sessions/{key}
// 后端两个端点 #783 后都还在（backend/emby_server/portal.py），前端 wrapper 在 api/index.ts
const sessions = ref<MyPlaybackSession[]>([])
const sessionsLoading = ref(true)
const sessionsError = ref(false)
const stopping = ref<string | null>(null)
const sessionToStop = ref<MyPlaybackSession | null>(null)
/** R50：结束播放失败时的弹窗内错误（失败不自动关弹窗，展示错误+重试） */
const stopError = ref('')

function typeLabel(t?: string | null) {
  // R41：Episode 标"单集" —— 之前错标成"剧集"，跟 Series 重了；与 CalendarView 同名函数对齐
  return ({ Movie: '电影', Series: '剧集', Episode: '单集' } as Record<string, string>)[t || ''] || '影片'
}

function fmtTime(iso?: string | null) {
  if (!iso) return EMPTY_DASH
  const date = new Date(iso)
  // R50：非法日期直接返回占位，不渲染 NaN
  if (!isFinite(date.getTime())) return EMPTY_DASH
  const diff = Date.now() - date.getTime()
  const minutes = Math.floor(diff / 60000)
  if (minutes < 1) return '刚刚'
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  return `${Math.floor(hours / 24)} 天前`
}

async function loadSessions() {
  sessionsLoading.value = true
  sessionsError.value = false
  try {
    const res = await embyApi.getSessions()
    sessions.value = res.sessions || []
  } catch {
    sessions.value = []
    sessionsError.value = true
  } finally {
    sessionsLoading.value = false
  }
}

function confirmStopSession(session: MyPlaybackSession) {
  recordTrigger()
  stopError.value = ''
  sessionToStop.value = session
  focusOpenModalCard()
}

/** Esc 关闭各确认弹窗（照 WishlistView 模式） */
function onEsc(e: KeyboardEvent) {
  if (e.key !== 'Escape') return
  if (showSetPlayPwd.value) closeSetPlayPwd()
  else if (showUnlockConfirm.value) closeUnlockConfirm()
  else if (sessionToStop.value) closeStopSession()
}

/** R36：简易 focus trap —— Tab 在对话框内循环，不跑出对话框（照抄 WishlistView R27 标杆） */
function onModalKeydown(e: KeyboardEvent) {
  if (e.key !== 'Tab') return
  const card = (e.currentTarget as HTMLElement).querySelector('.modal-card')
  if (!card) return
  const els = Array.from(
    card.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])'),
  ).filter((el) => el.offsetParent !== null)
  if (els.length === 0) return
  const first = els[0]
  const last = els[els.length - 1]
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault()
    last.focus()
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault()
    first.focus()
  }
}

/** R36：弹窗打开初始聚焦 —— 焦点放进对话框首个可聚焦元素（Teleport 到 body 后用 query 定位） */
function focusOpenModalCard() {
  nextTick(() => {
    const el = document.querySelector<HTMLElement>(
      '.modal-mask .modal-card button:not([disabled]), .modal-mask .modal-card input:not([disabled])',
    )
    el?.focus()
  })
}
/** R38 P2-2：弹窗关闭后焦点回到触发按钮（setPlayPwd / unlock / stopSession 三处共用） */
const lastTrigger = ref<HTMLElement | null>(null)
function recordTrigger() { lastTrigger.value = document.activeElement as HTMLElement | null }
function restoreTriggerFocus() {
  const t = lastTrigger.value
  // R50：触发按钮仍在 DOM 才还回去（会话行被移除后 focus 会静默掉回 body）
  if (t && document.contains(t)) {
    t.focus?.()
    return
  }
  // 触发按钮不在了：焦点落到仍打开的弹窗内关闭按钮/标题（标题补 tabindex="-1" 使其可聚焦）
  const card = document.querySelector<HTMLElement>('.modal-mask .modal-card')
  const target = card?.querySelector<HTMLElement>('.op-btn.ghost') || card?.querySelector<HTMLElement>('.modal-title')
  if (target) {
    if (target.tagName === 'H3') target.setAttribute('tabindex', '-1')
    target.focus?.()
  }
}
function closeSetPlayPwd() { showSetPlayPwd.value = false; restoreTriggerFocus() }
function closeUnlockConfirm() { showUnlockConfirm.value = false; restoreTriggerFocus() }
function closeStopSession() {
  // R50：先落焦点再关弹窗——此时弹窗仍在 DOM，触发按钮若已被移除可落到弹窗关闭按钮/标题
  restoreTriggerFocus()
  sessionToStop.value = null
}

/** R41：三个 Teleport 弹窗（设置密码 / 解锁确认 / 结束播放确认）—— 任一打开都锁住背后页面滚动。
 *  代码路径上三者互斥（解锁弹窗只在没权限分支，另两个只在有权限分支；弹窗打开时页面被遮罩盖住点不到别的），
 *  但用 anyModalOpen 统一 watch 而不是各写一遍，万一叠加也锁不死：三个全关才释放。 */
const anyModalOpen = computed(
  () => showSetPlayPwd.value || showUnlockConfirm.value || sessionToStop.value !== null,
)
/** R50：body 滚动锁——保存打开前的值，关闭时恢复（避免与其他锁滚动逻辑互相覆盖） */
let savedBodyOverflow = ''
watch(anyModalOpen, (open) => {
  if (open) {
    savedBodyOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
  } else {
    document.body.style.overflow = savedBodyOverflow
  }
})
function openSetPlayPwd() {
  // R37 P3-9：重开弹窗清掉旧错误文案
  pwdError.value = ''
  recordTrigger()
  showSetPlayPwd.value = true
  focusOpenModalCard()
}
function openUnlockConfirm() { recordTrigger(); showUnlockConfirm.value = true; focusOpenModalCard() }

async function doStopSession() {
  const session = sessionToStop.value
  if (!session) return
  stopping.value = session.session_key
  stopError.value = ''
  try {
    await embyApi.stopSession(session.session_key)
    toast.success(`已结束「${session.device || '未知设备'}」上的播放`)
    sessions.value = sessions.value.filter((s) => s.session_key !== session.session_key)
    // R50：成功才关弹窗
    closeStopSession()
  } catch (err: unknown) {
    // Batch G：中文判断逻辑已收敛进 toUserMessage（中文 detail 透传），不再内联
    // R41：结束播放失败走 stopFailed —— "暂时没能保存"跟"结束播放"动作对不上
    // R50：失败不自动关弹窗，错误留在弹窗内展示并可重试（不再 toast）
    stopError.value = toUserMessage(err, 'stopFailed')
  } finally {
    stopping.value = null
  }
}

onMounted(() => {
  void refreshAccount()
  window.addEventListener('keydown', onEsc)
})
onUnmounted(() => {
  window.removeEventListener('keydown', onEsc)
  // R41：清掉复制提示的定时器（照抄 CompanionView 模式）
  window.clearTimeout(copyTimer)
  // R41：卸载时释放 body 滚动锁，避免锁死
  document.body.style.overflow = savedBodyOverflow
})

// 有查看权限才拉正在播放（解锁后翻成 granted 也会触发一次）
watch(viewGranted, (granted) => {
  if (granted) void loadSessions()
}, { immediate: true })
</script>

<style scoped>
/* 接线员：v4 纸片风 */
.op-rows {
  background: var(--paper-card);
  border: 1.5px solid var(--line);
  border-radius: var(--paper-r-lg, 12px);
  overflow: hidden;
}
.op-rows.loading { opacity: 0.6; pointer-events: none; }
.op-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px 16px;
}
.op-row + .op-row { border-top: 1px dashed var(--line); }
.op-label {
  flex: none;
  width: 64px;
  font-size: 12px;
  color: var(--ink2);
  letter-spacing: 1px;
}
.op-value {
  flex: 1;
  font-size: 14px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.op-value.mono { font-family: ui-monospace, monospace; font-size: 13px; }
.copy-btn {
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  /* 透明 padding + 负 margin：视觉 32×32 不变，点击区 44×44 */
  width: 44px;
  height: 44px;
  padding: 6px;
  margin: -6px;
  border: none;
  background: transparent;
  position: relative;
  color: var(--ink2);
  cursor: pointer;
}
.copy-btn::before {
  content: '';
  position: absolute;
  inset: 6px;
  border-radius: var(--paper-r-md, 10px);
  border: 1.5px solid var(--line);
  background: var(--paper-card);
}
.copy-btn > * { position: relative; z-index: 1; }
.copy-btn .ok { color: var(--green); }
.copy-btn:focus-visible { outline: 2px solid var(--red); outline-offset: 2px; }
.text-btn {
  flex: none;
  font-size: 13px;
  font-weight: 800;
  color: var(--red);
  background: transparent;
  border: none;
  cursor: pointer;
  /* 透明 padding + 负 margin：文字视觉不变，点击区扩到 ≥44px（R50：横向 4px→12px） */
  padding: 15px 12px;
  margin: -9px -8px;
}
.op-tip {
  font-size: 12px;
  color: var(--ink2);
  line-height: 1.8;
  margin: 10px 2px 0;
}
.unlock-desc { font-size: 14px; line-height: 1.8; margin: 0 0 14px; }
.unlock-desc strong { color: var(--red); }

/* 多服 */
.realm-list { display: flex; flex-direction: column; gap: 10px; }
.realm-item {
  background: var(--paper-card);
  border: 1.5px solid var(--line);
  border-radius: var(--paper-r-lg, 12px);
  padding: 12px 14px;
}
.realm-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
}
.realm-head strong { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.badge {
  font-size: 11px;
  font-weight: 800;
  border-radius: var(--paper-r-md, 10px);
  padding: 2px 10px;
  letter-spacing: 1px;
  white-space: nowrap;
}
.badge.free { background: rgba(127, 166, 217, 0.18); color: #3d6b9e; border: 1px solid #3d6b9e; }
.badge.ok { background: rgba(76, 175, 80, 0.14); color: #2e7d32; border: 1px solid #2e7d32; }
.badge.off { background: rgba(0, 0, 0, 0.06); color: var(--ink2); border: 1px solid var(--line); }
.realm-url {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
  font-size: 12px;
}
.realm-url .mono {
  flex: 1;
  font-family: ui-monospace, monospace;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.realm-url.muted { color: var(--ink2); }

.scheme-btns { display: flex; flex-wrap: wrap; gap: 10px; }
.scheme-btn {
  font-size: 14px;
  font-weight: 800;
  padding: 12px 20px;
  min-height: 44px;
  border-radius: var(--paper-r-lg, 12px);
  border: 2px solid var(--ink);
  background: var(--paper-card);
  color: var(--ink);
  cursor: pointer;
  box-shadow: 0 2px 0 rgba(0, 0, 0, 0.12);
}
.scheme-btn:active { transform: translateY(2px); box-shadow: none; }
.scheme-btn:focus-visible { outline: 2px solid var(--red); outline-offset: 2px; }

/* 小白指引 */
.guide-steps { display: flex; flex-direction: column; gap: 12px; }
.guide-step { display: flex; gap: 12px; align-items: flex-start; }
.step-num {
  flex: none;
  width: 28px;
  height: 28px;
  border-radius: var(--paper-r-full, 50%);
  background: var(--red);
  color: var(--on-dark, #fff);
  font-size: 14px;
  font-weight: 800;
  display: flex;
  align-items: center;
  justify-content: center;
}
.guide-step b { font-size: 14px; }
.guide-step p { font-size: 13px; color: var(--ink2); margin: 4px 0 0; line-height: 1.7; }

/* 正在播放（旧 PlaybackSessions 纸片风重写） */
.op-session-list { display: flex; flex-direction: column; gap: 10px; }
.op-session-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 14px;
  background: var(--paper-card);
  border: 1.5px solid var(--line);
  border-radius: var(--paper-r-lg, 12px);
}
.op-session-icon {
  flex: none;
  width: 34px;
  height: 34px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--paper-r-md, 10px);
  background: rgba(217, 72, 43, 0.1);
  color: var(--red);
}
.op-session-main { flex: 1; min-width: 0; }
.op-session-title {
  display: flex;
  align-items: center;
  gap: 7px;
  flex-wrap: wrap;
}
.op-session-name {
  font-size: 14px;
  font-weight: 700;
  color: var(--ink);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}
.op-session-title .badge.type {
  font-size: 11px;
  font-weight: 800;
  border-radius: var(--paper-r-md, 10px);
  padding: 2px 10px;
  letter-spacing: 1px;
  white-space: nowrap;
  background: rgba(46, 95, 163, 0.12);
  color: var(--blue);
  border: 1px solid var(--blue);
}
.op-session-title .badge.paused {
  font-size: 11px;
  font-weight: 800;
  border-radius: var(--paper-r-md, 10px);
  padding: 2px 10px;
  letter-spacing: 1px;
  white-space: nowrap;
  background: rgba(201, 155, 63, 0.14);
  color: var(--gold-deep);
  border: 1px solid var(--gold);
}
.op-session-meta {
  display: flex;
  align-items: center;
  gap: 5px;
  margin-top: 3px;
  font-size: 12px;
  color: var(--ink2);
  flex-wrap: wrap;
}
.op-session-meta .addr { display: inline-flex; align-items: center; gap: 3px; }
.op-session-meta .method { color: var(--blue); }
.meta-sep { opacity: 0.5; }
.op-session-progress {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 6px;
}
.op-session-progress .bar {
  flex: 1;
  max-width: 220px;
  height: 4px;
  border-radius: 2px;
  background: var(--paper2);
  overflow: hidden;
}
.op-session-progress .bar-fill { height: 100%; background: var(--red); transform-origin: left center; transition: transform .3s ease; }
.op-session-progress .bar-text { font-size: 12px; color: var(--ink2); white-space: nowrap; }
.op-stop-btn {
  flex: none;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 13px;
  font-weight: 800;
  padding: 12px 16px;
  min-height: 44px;
  box-sizing: border-box;
  min-width: 92px;
  white-space: nowrap;
  border-radius: var(--paper-r-md, 10px);
  border: 1.5px solid var(--red);
  background: var(--paper-card);
  color: var(--red);
  cursor: pointer;
}
.op-stop-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.op-stop-btn:focus-visible { outline: 2px solid var(--red); outline-offset: 2px; }
/* 正在播放标题旁刷新按钮（复用 PigeonView 样式，44px 触摸目标） */
.refresh-btn {
  width: 76px; height: 44px; padding: 0 14px; flex-shrink: 0; border-radius: var(--paper-r-lg, 12px);
  border: 1.5px solid var(--line); background: var(--paper-card);
  color: var(--ink2); font-size: 18px; cursor: pointer; white-space: nowrap;
}
.refresh-btn:disabled { opacity: 0.5; cursor: default; }
.refresh-btn.is-loading { font-size: 14px; }
.refresh-btn:focus-visible { outline: 2px solid var(--red); outline-offset: 2px; }
.op-empty {
  text-align: center;
  padding: 20px 0 8px;
  color: var(--ink2);
}
.op-empty p { font-size: 14px; margin: 8px 0 0; }
.op-empty .op-empty-sub { font-size: 13px; margin-top: 4px; }

/* 按钮 */
.op-btn {
  font-size: 15px;
  font-weight: 800;
  letter-spacing: 2px;
  text-indent: 2px;
  padding: 12px 28px;
  min-height: 44px;
  border-radius: var(--paper-r-lg, 12px);
  cursor: pointer;
  border: 2px solid var(--ink);
}
.op-btn.primary {
  background: var(--red);
  border-color: var(--red);
  color: var(--on-dark, #fff);
  box-shadow: 0 3px 0 var(--red-deep, #a33319);
}
.op-btn.primary:active { transform: translateY(2px); box-shadow: none; }
.op-btn.primary:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }
.op-btn.ghost { background: transparent; color: var(--ink2); }
.op-btn:focus-visible { outline: 2px solid var(--red); outline-offset: 2px; }

/* 弹窗 */
.modal-mask {
  /* Teleport 到 body 后会脱离 .v4-scope，导致纸色变量继承被切断，故在此补齐弹窗子树用到的变量；值逐字抄自 v4-paper.css 的 .v4-scope 块 */
  --paper-card: #FFFDF6;
  --ink: #2A2620;
  --ink2: #6B6257;
  --red: #D9482B;
  --red-deep: #A33319;
  --red-ink: #B93A22;
  --on-dark: #FFFFFF;
  --line: #D8CDB8;
  --paper-r-md: 10px;
  --paper-r-lg: 12px;
  --paper-r-xl: 16px;
  position: fixed;
  inset: 0;
  background: rgba(20, 16, 12, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
  z-index: 100;
}
.modal-card {
  width: 100%;
  max-width: 340px;
  background: var(--paper-card);
  border: 2px solid var(--ink);
  border-radius: var(--paper-r-xl, 16px);
  padding: 22px 20px;
  box-shadow: 4px 4px 0 rgba(0, 0, 0, 0.15);
  max-height: calc(100dvh - 48px);
  overflow-y: auto;
}
.modal-title { font-size: 17px; font-weight: 800; margin: 0 0 8px; letter-spacing: 1px; }
.modal-desc { font-size: 13px; color: var(--ink2); line-height: 1.7; margin: 0 0 14px; }
.modal-desc strong { color: var(--red-ink); }
.modal-input {
  width: 100%;
  box-sizing: border-box;
  font-size: 16px;
  padding: 12px 14px;
  border: 1.5px solid var(--line);
  border-radius: var(--paper-r-md, 10px);
  background: var(--paper-card);
  color: var(--ink);
  margin-bottom: 6px;
}
.modal-input:focus-visible { outline: 2px solid var(--red); outline-offset: 1px; }
.modal-error { font-size: 12px; color: var(--red-ink); margin: 4px 0 0; }
.modal-actions { display: flex; gap: 10px; margin-top: 14px; flex-wrap: wrap; }
.modal-actions .op-btn { flex: 1; }

.free-note p { font-size: 13px; color: var(--ink2); line-height: 1.8; margin: 10px 0 0; }

/* 减弱动效：按下不再下沉（照 TicketCard/ParcelBox 模式） */
@media (prefers-reduced-motion: reduce) {
  .scheme-btn:active, .op-btn.primary:active { transform: none; }
  .op-session-progress .bar-fill { transition: none; }
}
</style>
