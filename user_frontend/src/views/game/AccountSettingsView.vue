<template>
  <div class="v4-scope">
    <div class="v4-page">
      <!-- 日志头 -->
      <header class="v4-loghead">
        <span class="v4-stamp">{{ facilityStamp('account') }}</span>
        <h1>⚙️ 账号设置</h1>
        <p>改登录密码、看我的订阅、管我的设备 —— 账号自己的事都在这儿。</p>
      </header>

      <!-- 安全设置：修改登录密码（逻辑由 ProfileView 移植，authApi.changePassword） -->
      <section class="v4-sec" aria-label="安全设置">
        <div class="v4-sec-title">🔐 安全设置</div>
        <div class="v4-sec-sub">登录密码是进营地的钥匙，定期换一把更安心</div>
        <div class="v4-card acct-card">
          <button type="button" class="acct-row" @click="showChangePwd = true">
            <span class="acct-row-ic" aria-hidden="true"><Lock :size="16" /></span>
            <span class="acct-row-text">修改登录密码</span>
            <ChevronRight :size="15" class="acct-row-arrow" />
          </button>
        </div>
      </section>

      <!-- 我的订阅：直接复用 ProfileSubscriptions.vue（查看到期 / 历史 / 取消订阅） -->
      <section class="v4-sec" aria-label="我的订阅">
        <div class="v4-sec-sub">订阅到期、历史账单、取消续订都在这里</div>
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

      <!-- 我的设备：第三方播放器登录设备列表 / 移除（顶上限自救通道，deviceApi） -->
      <section class="v4-sec" aria-label="我的设备">
        <div class="v4-sec-title">📱 我的设备</div>
        <div class="v4-sec-sub">用第三方播放器登录过的设备都在这里，顶到上限自己动手清</div>
        <div class="v4-card">
          <div class="dev-head">
            <span v-if="deviceInfo && deviceInfo.limit" class="dev-count">
              {{ deviceInfo.active_count }} / {{ deviceInfo.limit }} 台
            </span>
            <button type="button" class="v4-mini-btn" title="刷新" @click="loadDevices">
              <RefreshCw :size="13" :class="{ spinning: deviceLoading }" /> 刷新
            </button>
          </div>
          <div v-if="deviceLoading && !devices.length" class="v4-skeleton" style="height:120px"></div>
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
                {{ pendingRemove === d.device_id ? '确认移除' : '移除' }}
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
    <PanelTabbar />

    <!-- 修改登录密码弹窗（由 ProfileView 移植） -->
    <div v-if="showChangePwd" class="acct-mask" @click.self="showChangePwd = false">
      <div class="acct-modal" role="dialog" aria-modal="true" aria-label="修改登录密码">
        <h3 class="acct-modal-title">修改登录密码</h3>
        <div class="acct-field">
          <label class="acct-label">当前密码</label>
          <input v-model="pwdForm.oldPassword" type="password" autocomplete="current-password" />
        </div>
        <div class="acct-field">
          <label class="acct-label">新密码</label>
          <input v-model="pwdForm.newPassword" type="password" autocomplete="new-password" />
        </div>
        <div class="acct-field">
          <label class="acct-label">确认新密码</label>
          <input v-model="pwdForm.confirmPassword" type="password" autocomplete="new-password" />
        </div>
        <p v-if="pwdError" class="acct-error">{{ pwdError }}</p>
        <div class="acct-modal-actions">
          <button type="button" class="acct-btn ghost" @click="showChangePwd = false">取消</button>
          <button type="button" class="acct-btn primary" :disabled="pwdLoading" @click="handleChangePassword">
            {{ pwdLoading ? '提交中' : '确认修改' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 账号设置设施（/game/account）—— 旧「个人中心」ProfileView 的 P0 功能承接页。
 *
 * 只放三件套（移植 ProfileView 逻辑，不重写）：
 * 1. 修改登录密码（authApi.changePassword，全仓唯一入口）
 * 2. 我的订阅（复用 components/profile/ProfileSubscriptions.vue：到期 / 历史 / 取消）
 * 3. 我的设备（deviceApi：设备列表 / 移除，顶上限自救通道）
 *
 * 明确暂无承接（随旧页删除暂时失去）：Emby 多服列表、正在播放远程终止。
 */
import { computed, onMounted, ref } from 'vue'
import { Lock, ChevronRight, RefreshCw } from 'lucide-vue-next'
import PanelTabbar from '@/components/panel/PanelTabbar.vue'
import ProfileSubscriptions from '@/components/profile/ProfileSubscriptions.vue'
import { facilityStamp } from '@/config/game'
import { authApi, subscriptionApi, isExpiringSoon, type MySubscription } from '@/api'
import { deviceApi, type MyDevice, type MyDevicesResponse } from '@/api/economy'
import { useUserStore } from '@/stores/user'
import { useToast } from '@/composables/useToast'
import { toastCopy, confirmCopy } from '@/constants/toastMessages'
import { toUserMessage } from '@/utils/toUserMessage'

const userStore = useUserStore()
const toast = useToast()

// ===== 修改登录密码（ProfileView 移植） =====
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

// ===== 我的订阅（ProfileView 移植；展示层全在 ProfileSubscriptions.vue） =====
const subscriptions = ref<MySubscription[]>([])
const cancellingSub = ref<number | null>(null)

const activeSub = computed(
  () => subscriptions.value.find(s => s.status === 'active' && s.days_left > 0) || null,
)
const expiringSoon = computed(() => isExpiringSoon(activeSub.value))
const isFreeRealm = computed(() => userStore.isFreeRealm)
const realmNote = computed(() => userStore.realmNote)

async function loadSubs() {
  try {
    subscriptions.value = await subscriptionApi.getMine()
  } catch {
    // 订阅加载失败不阻塞页面
  }
}

async function cancelSubscription(s: MySubscription) {
  if (!confirm(confirmCopy.cancelSubscription(s.plan_name))) return
  cancellingSub.value = s.id
  try {
    const res = await subscriptionApi.cancel(s.id)
    toast.success(res.message || toastCopy.subscriptionCancelled())
    await loadSubs()
  } catch (err: any) {
    toast.error(toUserMessage(err, 'cancelFailed'))
  } finally {
    cancellingSub.value = null
  }
}

// ===== 我的设备（ProfileView 移植：列表 / 两步确认移除） =====
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

onMounted(() => {
  loadSubs()
  loadDevices()
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
  border-radius: 8px;
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
  border-radius: 8px;
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
  padding: 5px 10px;
  background: var(--paper2);
  border: 1px solid var(--line);
  border-radius: 999px;
  color: var(--ink2);
  font-size: 12px;
  cursor: pointer;
}
.spinning { animation: v4-spin 0.9s linear infinite; }
@keyframes v4-spin { to { transform: rotate(360deg); } }

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
  border-radius: 999px;
  font-size: 12px;
  white-space: nowrap;
  background: var(--paper2);
  color: var(--ink2);
}
.dev-badge.ok { background: var(--au-success-soft); color: var(--green); }
.dev-badge.off { background: var(--au-danger-soft); color: var(--red); }
.dev-remove {
  flex-shrink: 0;
  padding: 6px 10px;
  background: transparent;
  border: none;
  border-radius: 8px;
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
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: var(--scrim);
  z-index: 100;
}
.acct-modal {
  width: 100%;
  max-width: 340px;
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
  height: 42px;
  padding: 0 12px;
  background: var(--paper);
  border: 1px solid var(--line);
  border-radius: 8px;
  color: var(--ink);
  font-size: 14px;
  outline: none;
  box-sizing: border-box;
}
.acct-field input:focus { border-color: var(--ink); }
.acct-error {
  margin: 0 0 10px;
  padding: 8px 12px;
  background: var(--au-danger-soft);
  border-radius: 8px;
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
  height: 40px;
  padding: 0 16px;
  border-radius: 10px;
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
.acct-btn.primary:disabled { opacity: 0.5; cursor: not-allowed; }

@media (prefers-reduced-motion: reduce) {
  .spinning { animation-duration: 2.4s; }
  .acct-row:active, .dev-remove:active:not(:disabled) { background: transparent; }
}
</style>
