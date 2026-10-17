<template>
  <div class="v4-scope">
    <div class="v4-page">
      <!-- 日志头 -->
      <header class="v4-loghead">
        <span class="v4-stamp">{{ facilityStamp('account') }}</span>
        <h1><span aria-hidden="true">⚙️</span> 账号设置</h1>
        <p>改登录密码、看我的会员、管我的设备 —— 账号自己的事都在这儿。</p>
      </header>

      <!-- 安全设置：修改登录密码（逻辑由 ProfileView 移植，authApi.changePassword） -->
      <section class="v4-sec" aria-label="安全设置">
        <div class="v4-sec-title"><span aria-hidden="true">🔐</span> 安全设置</div>
        <div class="v4-sec-sub">登录密码是进营地的钥匙，定期换一把更安心</div>
        <div class="v4-card acct-card">
          <button type="button" class="acct-row" @click="openChangePwd">
            <span class="acct-row-ic" aria-hidden="true"><Lock :size="16" /></span>
            <span class="acct-row-text">修改登录密码</span>
            <ChevronRight :size="15" class="acct-row-arrow" />
          </button>
        </div>
      </section>

      <!-- 我的会员：直接复用 ProfileSubscriptions.vue（查看到期 / 历史 / 取消订阅） -->
      <section class="v4-sec" aria-label="我的会员">
        <!-- R41：补上标题行，与其他两节结构对齐（标题+副标题） -->
        <div class="v4-sec-title"><span aria-hidden="true">🏷️</span> 我的会员</div>
        <div class="v4-sec-sub">会员到期、历史账单、取消续订都在这里</div>
        <div v-if="subsError" class="v4-err">订阅加载失败，请稍后重试<br><button class="v4-retry" @click="loadSubs">重试</button></div>
        <!-- R41：在途显示骨架屏 —— 请求还没回来时别闪现"还没有会员"（ProfileSubscriptions 的空数据文案） -->
        <div v-else-if="subsLoading" class="v4-skeleton" style="height:180px"></div>
        <ProfileSubscriptions
          v-else
          :active-sub="activeSub"
          :subscriptions="subscriptions"
          :expiring-soon="expiringSoon"
          :is-free-realm="isFreeRealm"
          :realm-note="realmNote"
          :cancelling-sub="cancellingSub"
          @cancel="cancelSubscription"
        />
      </section>

      <!-- 发现页卡片展示：服务器卡片怎么摆，用户自选（模式 + 每服显隐，存 localStorage） -->
      <section class="v4-sec" aria-label="发现页卡片展示">
        <div class="v4-sec-title">发现页卡片展示</div>
        <div class="v4-sec-sub">发现页服务器卡片怎么摆，你说了算</div>
        <div class="v4-card">
          <div class="mode-seg" role="group" aria-label="发现页卡片展示模式">
            <button
              v-for="opt in modeOptions"
              :key="opt.v"
              type="button"
              :class="{ 'is-on': mode === opt.v }"
              :aria-pressed="mode === opt.v"
              @click="setMode(opt.v)"
            >
              {{ opt.label }}
            </button>
          </div>
          <p class="mode-hint">自动 = 一个服大卡、多个服总览</p>

          <div v-if="serversLoading && !servers.length" class="srv-empty">正在加载…</div>
          <div v-else-if="serversLoadError" class="srv-empty">服务器清单加载失败<button class="v4-retry" @click="() => loadServers()">重试</button></div>
          <div v-else-if="!servers.length" class="srv-empty">还没有可展示的服务器</div>
          <div v-else>
            <div v-for="s in servers" :key="s.id" class="srv-row">
              <div class="srv-info">
                <div class="srv-name">{{ s.name }}</div>
                <div class="srv-tag">{{ serverTagText(s) }}</div>
              </div>
              <button
                type="button"
                class="srv-switch"
                :class="{ 'is-on': cardPrefs.isVisible(s.id) }"
                role="switch"
                :aria-checked="cardPrefs.isVisible(s.id)"
                :aria-label="`${s.name}：在发现页显示`"
                @click="toggleServer(s.id)"
              ></button>
            </div>
          </div>
        </div>
      </section>

      <!-- 隐私设置：观影碑时长榜上榜开关（存账号） -->
      <section class="v4-sec" aria-label="隐私设置">
        <div class="v4-sec-title"><span aria-hidden="true">🙈</span> 隐私设置</div>
        <div class="v4-sec-sub">谁能在榜单上看到你，你说了算</div>
        <div class="v4-card">
          <div class="srv-row">
            <div class="srv-info">
              <div class="srv-name">观影碑时长榜上榜</div>
              <div class="srv-tag">打开后，你的观影时长会出现在世界观影碑的时长榜上（只显示昵称）；关掉则不上榜，但你自己仍能看到自己的排名</div>
            </div>
            <button
              type="button"
              class="srv-switch"
              :class="{ 'is-on': showOnLeaderboard }"
              :disabled="privacyLoading"
              role="switch"
              :aria-checked="showOnLeaderboard"
              aria-label="观影碑时长榜上榜"
              @click="toggleLeaderboard"
            ></button>
          </div>
          <p v-if="privacyError" class="srv-empty">{{ privacyError }}<button class="v4-retry" @click="loadPrivacy">重试</button></p>
        </div>
      </section>

      <!-- 我的设备：第三方播放器登录设备列表 / 移除（顶上限自救通道，deviceApi） -->
      <section class="v4-sec" aria-label="我的设备">
        <div class="v4-sec-title"><span aria-hidden="true">📱</span> 我的设备</div>
        <div class="v4-sec-sub">用第三方播放器登录过的设备都在这里，顶到上限自己动手清</div>
        <div class="v4-card">
          <div class="dev-head">
            <span v-if="deviceInfo && deviceInfo.limit" class="dev-count">
              {{ deviceInfo.active_count ?? 0 }} / {{ deviceInfo.limit }} 台
            </span>
            <!-- R38 P3-2：忙碌时文案「刷新中」替代旋转图标（旋转+reduce 伪守卫已删） -->
            <button type="button" class="v4-mini-btn" title="刷新" :disabled="deviceLoading" :aria-busy="deviceLoading" @click="loadDevices">
              <RefreshCw :size="13" /> {{ deviceLoading ? '刷新中' : '刷新' }}
            </button>
          </div>
          <div v-if="deviceLoading && !devices.length" class="v4-skeleton" style="height:120px"></div>
          <div v-else-if="deviceError" class="v4-err">设备列表加载失败，请稍后重试<br><button class="v4-retry" @click="loadDevices">重试</button></div>
          <div v-else-if="!devices.length" class="v4-empty">
            还没有设备记录 —— 用 Emby 客户端（Infuse / Forward 等）登录后会出现在这里
          </div>
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
              <span v-if="d.is_blocked" class="dev-badge off">已禁用</span>
              <span v-else-if="!d.is_online_recent" class="dev-badge idle">已闲置</span>
              <span v-else class="dev-badge ok">活跃</span>
              <button
                type="button"
                class="dev-remove"
                :class="{ confirming: pendingRemove === d.device_id }"
                :disabled="removingDevice === d.device_id"
                @click="handleRemoveDevice(d)"
              >
                <!-- R37 P3-8：移除忙碌态给人话文案「移除中」，不再只有变暗 -->
                {{ removingDevice === d.device_id ? '移除中' : (pendingRemove === d.device_id ? '确认移除' : '移除') }}
              </button>
            </div>
          </template>
          <p class="dev-tip">
            <template v-if="deviceInfo && deviceInfo.limit">
              账号最多同时使用 {{ deviceInfo.limit }} 台设备（近 {{ deviceInfo.active_days }} 天内登录过）。
              {{ deviceInfo.auto_evict ? '超限时会自动停用最久未用的设备。' : '超限时请先移除不再使用的设备。' }}
            </template>
            <template v-else>移除后该设备登录状态立即失效，需重新输入账号密码。</template>
          </p>
        </div>
      </section>
    </div>

    <!-- 修改登录密码弹窗（由 ProfileView 移植） -->
    <div v-if="showChangePwd" class="acct-mask" @click.self="closeChangePwd" @keydown="onPwdModalKeydown">
      <div class="acct-modal" role="dialog" aria-modal="true" aria-label="修改登录密码">
        <h3 class="acct-modal-title">修改登录密码</h3>
        <div class="acct-field">
          <label class="acct-label" for="pwd-old">当前密码</label>
          <input id="pwd-old" ref="oldPwdInput" v-model="pwdForm.oldPassword" type="password" autocomplete="current-password" />
        </div>
        <div class="acct-field">
          <label class="acct-label" for="pwd-new">新密码</label>
          <input id="pwd-new" v-model="pwdForm.newPassword" type="password" autocomplete="new-password" />
        </div>
        <div class="acct-field">
          <label class="acct-label" for="pwd-confirm">确认新密码</label>
          <input id="pwd-confirm" v-model="pwdForm.confirmPassword" type="password" autocomplete="new-password" />
        </div>
        <p v-if="pwdError" class="acct-error" role="alert">{{ pwdError }}</p>
        <div class="acct-modal-actions">
          <button type="button" class="acct-btn ghost" :disabled="pwdLoading" @click="closeChangePwd">取消</button>
          <button type="button" class="acct-btn primary" :disabled="pwdLoading" @click="handleChangePassword">
            {{ pwdLoading ? '提交中' : '确认修改' }}
          </button>
        </div>
      </div>
    </div>

    <!-- R36：终止订阅二次确认（纸色风格，替代原生 confirm；照抄 SignalView R27） -->
    <Modal v-model="cancelConfirmOpen" size="sm" content-class="paper-confirm">
      <template #header>
        <h3 class="modal-title">终止订阅</h3>
      </template>
      <p class="acct-confirm-text">{{ pendingCancel ? confirmCopy.cancelSubscription(pendingCancel.plan_name) : '' }}</p>
      <template #footer>
        <div class="acct-modal-actions">
          <button type="button" class="acct-btn ghost" @click="cancelConfirmOpen = false">再想想</button>
          <button type="button" class="acct-btn primary" :disabled="cancellingSub !== null" @click="doCancelSubscription">
            {{ cancellingSub !== null ? '终止中' : '确定终止' }}
          </button>
        </div>
      </template>
    </Modal>
  </div>
</template>

<script setup lang="ts">
/**
 * 账号设置设施（/game/account）—— 旧「个人中心」ProfileView 的 P0 功能承接页。
 *
 * 只放三件套（移植 ProfileView 逻辑，不重写）：
 * 1. 修改登录密码（authApi.changePassword，全仓唯一入口）
 * 2. 我的会员（复用 components/profile/ProfileSubscriptions.vue：到期 / 历史 / 取消）
 * 3. 我的设备（deviceApi：设备列表 / 移除，顶上限自救通道）
 *
 * 后续已承接（R45）：Emby 多服列表、正在播放远程终止已由接线员页
 * （/game/operator）承接 —— realmCards 多服列表 + 正在播放一键远程结束。
 */
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { Lock, ChevronRight, RefreshCw } from 'lucide-vue-next'
import Modal from '@/components/ui/Modal.vue'
import ProfileSubscriptions from '@/components/profile/ProfileSubscriptions.vue'
import { facilityStamp } from '@/config/game'
import { authApi, subscriptionApi, isExpiringSoon, type MySubscription } from '@/api'
import { deviceApi, type MyDevice, type MyDevicesResponse } from '@/api/economy'
import { useUserStore } from '@/stores/user'
import { useToast } from '@/composables/useToast'
import { useServerCards, type ServerCardItem } from '@/composables/useServerCards'
import { useServerCardPrefs, type ServerCardMode } from '@/composables/useServerCardPrefs'
import { humanizeError } from '@/composables/usePanelApi'
import { panelApi } from '@/composables/usePanelApi'
import { toastCopy, confirmCopy } from '@/constants/toastMessages'
import { toUserMessage } from '@/utils/toUserMessage'

const userStore = useUserStore()
const toast = useToast()

// ===== 修改登录密码（ProfileView 移植） =====
const showChangePwd = ref(false)
const pwdForm = ref({ oldPassword: '', newPassword: '', confirmPassword: '' })
const pwdLoading = ref(false)
const pwdError = ref('')
const oldPwdInput = ref<HTMLInputElement | null>(null)

/** R36：改密码弹窗键盘闭环（照抄 WishlistView R27 标杆：focus trap + 自动聚焦 + Esc） */
function onPwdModalKeydown(e: KeyboardEvent) {
  if (e.key !== 'Tab' || !showChangePwd.value) return
  const card = (e.currentTarget as HTMLElement).querySelector('.acct-modal')
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
/** R38 P2-2：弹窗关闭后焦点回到触发按钮（改密码 / 终止订阅共用） */
const lastTrigger = ref<HTMLElement | null>(null)
function onPwdEsc(e: KeyboardEvent) {
  if (e.key === 'Escape' && showChangePwd.value) closeChangePwd()
}
function openChangePwd() {
  // R37 P3-9：重开弹窗清掉旧错误文案
  pwdError.value = ''
  // R38 P2-2：记住触发按钮，关闭后焦点回到它
  lastTrigger.value = document.activeElement as HTMLElement | null
  showChangePwd.value = true
  // R41：弹窗打开时锁住背后页面滚动（照抄 WishlistView R38 P3-11 模式）
  document.body.style.overflow = 'hidden'
  window.addEventListener('keydown', onPwdEsc)
  nextTick(() => oldPwdInput.value?.focus())
}
function closeChangePwd() {
  // R54-10：提交在途时不允许关闭，否则在途请求的失败原因会被清空后静默丢失
  if (pwdLoading.value) return
  showChangePwd.value = false
  // R41：弹窗关闭时释放 body 滚动锁（照抄 WishlistView R38 P3-11 模式）
  document.body.style.overflow = ''
  // R41：关闭时同步清空表单 —— 取消后重开，旧密码不再残留（pwdError 的清理保留在 openChangePwd）
  pwdForm.value = { oldPassword: '', newPassword: '', confirmPassword: '' }
  window.removeEventListener('keydown', onPwdEsc)
  lastTrigger.value?.focus?.()
}
onUnmounted(() => {
  window.removeEventListener('keydown', onPwdEsc)
  // R41：组件卸载时兜底释放 body 滚动锁，避免锁死
  document.body.style.overflow = ''
})

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
    closeChangePwd()
    pwdForm.value = { oldPassword: '', newPassword: '', confirmPassword: '' }
  } catch (err: any) {
    pwdError.value = toUserMessage(err, 'settingFailed')
  } finally {
    pwdLoading.value = false
  }
}

// ===== 我的会员（ProfileView 移植；展示层全在 ProfileSubscriptions.vue） =====
const subscriptions = ref<MySubscription[]>([])
const subsError = ref('')
// R41：在途标记 —— loadSubs 全程置位，在途显示 .v4-skeleton，不闪现"还没有会员"
const subsLoading = ref(false)
const cancellingSub = ref<number | null>(null)

const activeSub = computed(
  () => subscriptions.value.find(s => s.status === 'active' && s.days_left > 0) || null,
)
const expiringSoon = computed(() => isExpiringSoon(activeSub.value))
const isFreeRealm = computed(() => userStore.isFreeRealm)
const realmNote = computed(() => userStore.realmNote)

async function loadSubs() {
  subsLoading.value = true
  subsError.value = ''
  try {
    subscriptions.value = await subscriptionApi.getMine()
  } catch (e) {
    // 加载失败别装成"没订阅"，留错误态给用户重试
    subsError.value = humanizeError(e)
  } finally {
    subsLoading.value = false
  }
}

/** R36：终止订阅二次确认 —— 原生 confirm() 改用纸色 Modal（照抄 SignalView R27 先例）。
 *  文案继续走 confirmCopy.cancelSubscription，不改文案。 */
const pendingCancel = ref<MySubscription | null>(null)
const cancelConfirmOpen = computed({
  get: () => pendingCancel.value !== null,
  // R38 P2-2：任何关闭路径（"再想想"、Modal 内部关闭按钮/Esc）都把焦点还给触发按钮
  set: (v: boolean) => { if (!v) { pendingCancel.value = null; lastTrigger.value?.focus?.() } },
})
function cancelSubscription(s: MySubscription) {
  lastTrigger.value = document.activeElement as HTMLElement | null
  pendingCancel.value = s
}
async function doCancelSubscription() {
  const s = pendingCancel.value
  if (!s || cancellingSub.value !== null) return
  cancellingSub.value = s.id
  try {
    const res = await subscriptionApi.cancel(s.id)
    toast.success(res.message || toastCopy.subscriptionCancelled())
    cancelConfirmOpen.value = false
    await loadSubs()
  } catch (err: any) {
    toast.error(toUserMessage(err, 'cancelFailed'))
    // 失败时保持确认弹窗打开，用户可直接重试，不丢失上下文（照抄 SignalView R27）
  } finally {
    cancellingSub.value = null
  }
}

// ===== 我的设备（ProfileView 移植：列表 / 两步确认移除） =====
const devices = ref<MyDevice[]>([])
const deviceInfo = ref<MyDevicesResponse | null>(null)
const deviceLoading = ref(false)
const deviceError = ref('')
const removingDevice = ref('')
const pendingRemove = ref('')

async function loadDevices() {
  deviceLoading.value = true
  deviceError.value = ''
  try {
    const res = await deviceApi.mine()
    devices.value = res.devices
    deviceInfo.value = res
  } catch (e) {
    // 加载失败别装成"没设备"，留错误态给用户重试
    deviceError.value = humanizeError(e)
  } finally {
    deviceLoading.value = false
  }
}

async function handleRemoveDevice(device: MyDevice) {
  // R54-12：移除请求在途时直接短路，避免并发删除同一设备
  if (removingDevice.value) return
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
  if (Number.isNaN(diff)) return '—'
  if (diff < 0) return '刚刚'
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return '刚刚'
  if (mins < 60) return `${mins} 分钟前`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours} 小时前`
  return `${Math.floor(hours / 24)} 天前`
}

// ===== 发现页卡片展示（模式 + 每服显隐，偏好存 localStorage） =====
const cardPrefs = useServerCardPrefs()
const { mode, setMode, toggleServer } = cardPrefs
const { servers, load: loadServers, loading: serversLoading, loadError: serversLoadError } = useServerCards()

const modeOptions: { v: ServerCardMode; label: string }[] = [
  { v: 'auto', label: '自动' },
  { v: 'cards', label: '大卡' },
  { v: 'overview', label: '总览' },
]

const ACCESS_LABEL: Record<ServerCardItem['accessMode'], string> = {
  paid: '付费服',
  free: '公益服',
}

function serverTagText(s: ServerCardItem): string {
  return ACCESS_LABEL[s.accessMode]
}

/* ---------- 隐私：观影碑时长榜上榜（存账号） ---------- */
const showOnLeaderboard = ref(true)
const privacyLoading = ref(false)
const privacyError = ref('')

async function loadPrivacy() {
  privacyLoading.value = true
  privacyError.value = ''
  try {
    const d = await panelApi.privacyGet()
    showOnLeaderboard.value = d.show_on_leaderboard !== false
  } catch (e) {
    privacyError.value = humanizeError(e)
  } finally {
    privacyLoading.value = false
  }
}

async function toggleLeaderboard() {
  if (privacyLoading.value) return
  const next = !showOnLeaderboard.value
  privacyLoading.value = true
  try {
    const d = await panelApi.privacySet(next)
    showOnLeaderboard.value = d.show_on_leaderboard !== false
    toast.success(next ? '已允许上榜' : '已关掉上榜')
  } catch (e) {
    toast.error(humanizeError(e))
  } finally {
    privacyLoading.value = false
  }
}

onMounted(() => {
  loadSubs()
  loadDevices()
  loadPrivacy()
  if (!serversLoading.value && !servers.value.length) void loadServers()
})
</script>

<style scoped>
/* 账号设置设施：v4 纸色（只用 v4-paper.css 定义的 token，不硬编码色值） */

.acct-card { padding: 6px; }

.acct-row {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  min-height: 52px;
  padding: 10px 12px;
  background: transparent;
  border: none;
  border-radius: var(--paper-r-sm, 8px);
  cursor: pointer;
  color: var(--ink);
  font-size: 15px;
  text-align: left;
}
.acct-row:active { background: var(--paper2); }

.acct-row-ic {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  flex-shrink: 0;
  border-radius: var(--paper-r-sm, 8px);
  background: var(--paper2);
  color: var(--ink2);
}
.acct-row-text { flex: 1; font-weight: 500; }
.acct-row-arrow { color: var(--ink2); flex-shrink: 0; }

/* ---- 我的设备 ---- */
.dev-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
}
.dev-count {
  font-size: 12px;
  color: var(--ink2);
  font-variant-numeric: tabular-nums;
}
.v4-mini-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  /* R41：纵向 padding 13px→15px —— 原注释声称 44px，实算约 40px，提到 15px 才诚实达标（视觉不变） */
  padding: 15px 10px;
  margin: -8px 0;
  background: var(--paper2);
  border: 1px solid var(--line);
  border-radius: var(--paper-r-full, 999px);
  color: var(--ink2);
  font-size: 12px;
  cursor: pointer;
}
/* R38 P3-2：.spinning 旋转动画已删（忙碌改走文案「刷新中」）；reduce 伪守卫一并删 */

.dev-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 0;
}
.dev-row + .dev-row { border-top: 1px solid var(--line); }
.dev-main {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  flex: 1;
}
.dev-name {
  font-size: 14px;
  font-weight: 500;
  color: var(--ink);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.dev-meta {
  font-size: 12px;
  color: var(--ink2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
.dev-badge {
  flex-shrink: 0;
  padding: 2px 8px;
  border-radius: var(--paper-r-full, 999px);
  font-size: 12px;
  white-space: nowrap;
  background: var(--paper2);
  color: var(--ink2);
}
.dev-badge.ok { background: var(--au-success-soft); color: var(--green); }
.dev-badge.off { background: var(--au-danger-soft); color: var(--red); }
.dev-remove {
  flex-shrink: 0;
  /* R41：纵向 padding 13px→15px —— 原注释声称 44px，实算约 40px，提到 15px 才诚实达标（视觉不变） */
  padding: 15px 10px;
  margin: -7px 0;
  background: transparent;
  border: none;
  border-radius: var(--paper-r-sm, 8px);
  color: var(--ink2);
  font-size: 13px;
  white-space: nowrap;
  cursor: pointer;
}
.dev-remove:active:not(:disabled) { background: var(--paper2); }
.dev-remove:disabled { opacity: 0.5; cursor: not-allowed; }
.dev-remove.confirming { color: var(--red); font-weight: 600; }
.dev-tip {
  margin: 10px 0 0;
  padding-top: 10px;
  border-top: 1px solid var(--line);
  font-size: 12px;
  line-height: 1.6;
  color: var(--ink2);
}

/* ---- 改密码弹窗（纸色） ---- */
.acct-mask {
  position: fixed;
  inset: 0;
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding: 24px;
  padding: max(24px, env(safe-area-inset-top)) max(24px, env(safe-area-inset-right)) max(24px, env(safe-area-inset-bottom)) max(24px, env(safe-area-inset-left));
  overflow-y: auto;
  background: var(--scrim);
  z-index: 100;
}
.acct-modal {
  width: 100%;
  max-width: 340px;
  margin: auto;
  max-height: calc(100% - 48px);
  max-height: calc(100dvh - 48px);
  overflow-y: auto;
  padding: 20px;
  background: var(--paper-card);
  border: 1.5px solid var(--ink);
  border-radius: 14px;
  box-shadow: 4px 4px 0 rgba(42, 38, 32, 0.15);
}
.acct-modal-title {
  margin: 0 0 14px;
  font-size: 17px;
  font-weight: 600;
  color: var(--ink);
}
.acct-field { margin-bottom: 12px; }
.acct-label {
  display: block;
  margin-bottom: 6px;
  font-size: 12px;
  color: var(--ink2);
}
.acct-field input {
  width: 100%;
  height: 44px;
  padding: 0 12px;
  background: var(--paper);
  border: 1px solid var(--line);
  border-radius: var(--paper-r-sm, 8px);
  color: var(--ink);
  font-size: 16px;
  outline: none;
  box-sizing: border-box;
}
.acct-field input:focus { border-color: var(--ink); }
.acct-field input:focus-visible { outline: 2px solid var(--ink); outline-offset: 1px; }
.acct-error {
  margin: 0 0 10px;
  padding: 8px 12px;
  background: var(--au-danger-soft);
  border-radius: var(--paper-r-sm, 8px);
  color: var(--red);
  font-size: 13px;
}
.acct-modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 16px;
}
.acct-btn {
  height: 44px;
  padding: 0 16px;
  border-radius: var(--paper-r-md, 10px);
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
}
.acct-btn.ghost {
  background: var(--paper2);
  border: 1px solid var(--line);
  color: var(--ink2);
}
.acct-btn.primary {
  background: var(--ink);
  border: 1px solid var(--ink);
  color: var(--on-dark);
  font-weight: 600;
}
.acct-btn:disabled { opacity: 0.5; cursor: not-allowed; }
/* R36：终止订阅确认弹窗的正文（走 .paper-confirm 纸色弹窗，文字排版本地样式） */
.acct-confirm-text { font-size: 14px; line-height: 1.9; color: var(--ink); margin: 0; }

/* 发现页卡片展示：模式三段选择 + 每服显隐开关（只用 --paper-* token） */
.mode-seg {
  display: flex;
  border: 1.5px solid var(--paper-ink);
  border-radius: 8px;
  overflow: hidden;
}
.mode-seg button {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 44px;
  background: none;
  border: 0;
  font-size: 12px;
  color: var(--paper-ink);
  cursor: pointer;
}
.mode-seg button + button {
  border-left: 1px solid var(--paper-line);
}
.mode-seg button.is-on {
  background: var(--paper-primary);
  color: #2A2620;
  font-weight: 700;
}
.mode-hint {
  margin: 8px 0 0;
  font-size: 11px;
  color: var(--paper-ink-2);
}
.srv-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 0;
}
.srv-row + .srv-row {
  border-top: 1px dashed var(--paper-line);
}
.srv-name {
  font-size: 14px;
  font-weight: 700;
  color: var(--paper-ink);
}
.srv-tag {
  font-size: 11px;
  color: var(--paper-ink-2);
}
.srv-switch {
  position: relative;
  flex: none;
  width: 48px;
  height: 44px;
  padding: 0;
  border-radius: 999px;
  border: 0;
  background: none;
  cursor: pointer;
}
.srv-switch::before {
  content: '';
  position: absolute;
  left: 6px;
  top: 12px;
  width: 36px;
  height: 20px;
  border-radius: 999px;
  border: 1.5px solid var(--paper-ink);
  background: var(--paper-card);
}
.srv-switch.is-on::before {
  background: var(--paper-primary);
}
.srv-switch::after {
  content: '';
  position: absolute;
  top: 14px;
  left: 8px;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: var(--paper-card);
  border: 1.5px solid var(--paper-ink);
}
.srv-switch.is-on::after {
  left: auto;
  right: 8px;
  background: var(--paper-ink);
}
.srv-empty {
  padding: 10px 0;
  font-size: 12px;
  color: var(--paper-ink-2);
}
</style>
