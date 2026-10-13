<template>
  <div class="v4-scope">
    <div class="v4-page">
      <!-- 日志头 -->
      <header class="v4-loghead">
        <span class="v4-stamp">营地 · 接线处</span>
        <h1>🔌 接线员</h1>
        <p>买完会员来这儿接线 —— 拿上账号密码，去播放器里连上就能看。</p>
      </header>

      <!-- 加载中 -->
      <div v-if="loading && !account" class="v4-sec">
        <div class="v4-skeleton" style="height:180px"></div>
      </div>

      <!-- 加载失败 -->
      <div v-else-if="accountError && !account" class="v4-sec">
        <div class="v4-err">
          接线员没接上：{{ accountError }}<br>
          <button class="v4-retry" @click="refreshAccount">重试</button>
        </div>
      </div>

      <template v-else>
        <!-- 公益服说明 -->
        <div v-if="isFreeRealm" class="v4-sec">
          <div class="v4-sec-title">📻 接入方式</div>
          <div class="free-note">
            <span class="badge free">公益服 · 免费开放</span>
            <p>{{ realmNote }}</p>
          </div>
        </div>

        <!-- 有查看权限：账号密码卡 -->
        <template v-if="viewGranted">
          <section class="v4-sec">
            <div class="v4-sec-title">🔑 你的接线账号</div>
            <div class="v4-sec-sub">拿着这三样，去播放器里登录</div>

            <div class="op-rows" :class="{ loading }">
              <div class="op-row">
                <span class="op-label">服务器</span>
                <span class="op-value mono">{{ serverUrl }}</span>
                <button class="copy-btn" title="复制服务器地址" @click="copyText(serverUrl, 'server')">
                  <Check v-if="copiedField === 'server'" :size="14" class="ok" />
                  <Copy v-else :size="14" />
                </button>
              </div>
              <div class="op-row">
                <span class="op-label">用户名</span>
                <span class="op-value mono">{{ embyUsername }}</span>
                <button class="copy-btn" title="复制用户名" @click="copyText(embyUsername, 'user')">
                  <Check v-if="copiedField === 'user'" :size="14" class="ok" />
                  <Copy v-else :size="14" />
                </button>
              </div>
              <div class="op-row">
                <span class="op-label">播放密码</span>
                <span class="op-value mono">
                  <template v-if="hasPlayPassword">{{ showPlayPassword ? playPasswordText : '••••••••' }}</template>
                  <template v-else>未设置</template>
                </span>
                <button
                  v-if="hasPlayPassword"
                  class="copy-btn"
                  :title="showPlayPassword ? '隐藏' : '查看'"
                  @click="showPlayPassword = !showPlayPassword"
                >
                  <Eye v-if="showPlayPassword" :size="14" />
                  <EyeOff v-else :size="14" />
                </button>
                <button class="text-btn" @click="showSetPlayPwd = true">
                  {{ hasPlayPassword ? '修改' : '设置' }}
                </button>
              </div>
            </div>

            <p class="op-tip">
              播放密码用于 Emby 客户端登录，和这个网站的登录密码是两回事，互不影响。
            </p>
          </section>

          <!-- 多服 -->
          <section v-if="realmCards.length > 1" class="v4-sec">
            <div class="v4-sec-title">🗺️ 我的服</div>
            <div class="v4-sec-sub">每个服有自己的地址和会员</div>
            <div class="realm-list">
              <div v-for="r in realmCards" :key="r.id" class="realm-item">
                <div class="realm-head">
                  <strong>{{ r.name }}</strong>
                  <span v-if="r.is_free" class="badge free">公益服</span>
                  <span v-else-if="r.subscribed" class="badge ok">会员剩 {{ daysLeft(r.end_date) }} 天</span>
                  <span v-else class="badge off">未开通</span>
                </div>
                <div v-if="r.view_granted" class="realm-url">
                  <span class="mono">{{ r.base_url || '管理员还没填这个服的地址' }}</span>
                  <button
                    v-if="r.base_url"
                    class="copy-btn"
                    title="复制"
                    @click="copyText(r.base_url, `realm-${r.id}`)"
                  >
                    <Check v-if="copiedField === `realm-${r.id}`" :size="13" class="ok" />
                    <Copy v-else :size="13" />
                  </button>
                </div>
                <div v-else class="realm-url muted">
                  <span>{{ r.is_free ? '花积分解锁后可见' : '开通会员后可见' }}</span>
                </div>
              </div>
            </div>
          </section>

          <!-- 一键导入 -->
          <section v-if="hasSchemes" class="v4-sec">
            <div class="v4-sec-title">📲 一键导入到客户端</div>
            <div class="v4-sec-sub">点一下，直接跳到播放器里填好</div>
            <input
              v-model="importPassword"
              type="password"
              class="import-pwd-input"
              placeholder="输入播放密码一起导入（可选）"
              autocomplete="off"
            />
            <div class="scheme-btns">
              <button
                v-for="(url, name) in importSchemes"
                :key="name"
                class="scheme-btn"
                @click="openScheme(url)"
              >
                {{ name }}
              </button>
            </div>
            <p class="op-tip">密码只放在这一次跳转里，不会存下来，关掉页面就没了。</p>
          </section>

          <!-- 怎么连：小白指引 -->
          <section class="v4-sec">
            <div class="v4-sec-title">🧭 怎么连上？</div>
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
          <div class="v4-sec-title">🔒 接线需要解锁</div>
          <template v-if="viewPermission.realm_free">
            <p class="unlock-desc">
              花 <strong>{{ viewPermission.unlock_points }}</strong> 积分解锁查看权限，
              有效期 {{ viewPermission.unlock_days > 0 ? viewPermission.unlock_days + ' 天' : '永久' }}。
              你当前有 <strong>{{ viewPermission.points_balance }}</strong> 积分。
            </p>
            <button
              class="op-btn primary"
              :disabled="unlocking || viewPermission.points_balance < viewPermission.unlock_points"
              @click="showUnlockConfirm = true"
            >
              {{ viewPermission.points_balance < viewPermission.unlock_points ? '积分不足' : `花 ${viewPermission.unlock_points} 积分解锁` }}
            </button>
            <p class="op-tip">积分可通过每日报到、邀请好友等方式获得。</p>
          </template>
          <template v-else>
            <p class="unlock-desc">服务器地址和账号只向会员开放，开通后回来这儿接线。</p>
            <button class="op-btn primary" @click="router.push('/store')">去开通会员</button>
          </template>
        </div>

        <div v-else class="v4-sec">
          <div class="v4-skeleton" style="height:120px"></div>
        </div>
      </template>
    </div>

    <!-- 设置播放密码弹窗 -->
    <Teleport to="body">
      <div v-if="showSetPlayPwd" class="modal-mask" @click.self="showSetPlayPwd = false">
        <div class="modal-card" role="dialog" aria-modal="true" aria-label="设置播放密码">
          <h3 class="modal-title">设置 Emby 播放密码</h3>
          <p class="modal-desc">这个密码用在 Emby 客户端（Infuse、Forward 等）里登录，至少 6 位。</p>
          <input
            v-model="playPwd"
            type="password"
            class="modal-input"
            placeholder="输入新密码（至少 6 位）"
            autocomplete="new-password"
          />
          <p v-if="pwdError" class="modal-error">{{ pwdError }}</p>
          <div class="modal-actions">
            <button class="op-btn ghost" @click="showSetPlayPwd = false">取消</button>
            <button class="op-btn primary" :disabled="settingPwd" @click="confirmSetPassword">
              {{ settingPwd ? '设置中…' : '确认' }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- 解锁确认弹窗 -->
    <Teleport to="body">
      <div v-if="showUnlockConfirm" class="modal-mask" @click.self="showUnlockConfirm = false">
        <div class="modal-card" role="dialog" aria-modal="true" aria-label="确认解锁">
          <h3 class="modal-title">确认解锁？</h3>
          <p class="modal-desc">
            将花费 <strong>{{ viewPermission?.unlock_points }}</strong> 积分解锁查看权限。
          </p>
          <div class="modal-actions">
            <button class="op-btn ghost" @click="showUnlockConfirm = false">取消</button>
            <button class="op-btn primary" :disabled="unlocking" @click="confirmUnlock">
              {{ unlocking ? '解锁中…' : '确认' }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>

    <PanelTabbar />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Check, Copy, Eye, EyeOff } from 'lucide-vue-next'
import PanelTabbar from '@/components/panel/PanelTabbar.vue'
import { useToast } from '@/composables/useToast'
import { humanizeError } from '@/composables/usePanelApi'
import {
  embyApi,
  type AccountCard, type AccountRealmCard, type ViewPermission,
} from '@/api'
import { useUserStore } from '@/stores/user'

const router = useRouter()
const toast = useToast()
const userStore = useUserStore()

const loading = ref(true)
const account = ref<AccountCard | null>(null)
const accountError = ref('')
const viewPermission = ref<ViewPermission | null>(null)
const unlocking = ref(false)
const showUnlockConfirm = ref(false)

const copiedField = ref('')
const showPlayPassword = ref(false)
const playPasswordText = ref('')
const showSetPlayPwd = ref(false)
const playPwd = ref('')
const pwdError = ref('')
const settingPwd = ref(false)
const importPassword = ref('')

/** 是否有查看权限：后端下发了 account 即视为有 */
const viewGranted = computed(() => !!account.value && !accountError.value)
const hasPlayPassword = computed(() => !!(account.value as any)?.has_play_password)

const serverUrl = computed(() => (account.value as any)?.server_url || (account.value as any)?.base_url || '—')
const embyUsername = computed(() => {
  const a = account.value as any
  return a?.emby_username || userStore.user?.username || '—'
})
const isFreeRealm = computed(() => !!(account.value as any)?.realm_free || !!(viewPermission.value as any)?.realm_free)
const realmNote = computed(() => (account.value as any)?.realm_note || (viewPermission.value as any)?.realm_note || '')
const realmCards = computed((): AccountRealmCard[] => {
  const r = (account.value as any)?.realms
  return Array.isArray(r) ? r : []
})
const importSchemes = computed((): Record<string, string> => {
  const s = (account.value as any)?.import_schemes
  return s && typeof s === 'object' ? s : {}
})
const hasSchemes = computed(() => Object.keys(importSchemes.value).length > 0)

async function refreshAccount() {
  loading.value = true
  accountError.value = ''
  try {
    const res = await embyApi.getAccountCard()
    account.value = res as AccountCard
    // 播放密码明文：后端只在已设置且用户主动查看时下发，这里按需懒加载
    if (hasPlayPassword.value) {
      try {
        const pwd = await (embyApi as any).getPlayPassword?.()
        if (typeof pwd === 'string') playPasswordText.value = pwd
      } catch { /* 看不到就显示已设置 */ }
    }
  } catch (e: any) {
    // 403 没权限 → 拿解锁信息
    const status = e?.response?.status ?? e?.status
    if (status === 403) {
      try {
        const perm = await embyApi.getViewPermission()
        viewPermission.value = perm as ViewPermission
      } catch { /* 拿不到权限信息就显示通用提示 */ }
      accountError.value = ''
    } else {
      accountError.value = humanizeError(e)
    }
  } finally {
    loading.value = false
  }
}

function copyText(text: string, field: string) {
  if (!text || text === '—') return
  navigator.clipboard?.writeText(text).then(
    () => {
      copiedField.value = field
      toast.success('已复制')
      setTimeout(() => { copiedField.value = '' }, 1500)
    },
    () => toast.error('复制失败，长按手动复制'),
  )
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
    showSetPlayPwd.value = false
    playPwd.value = ''
    showPlayPassword.value = false
    await refreshAccount()
  } catch (e) {
    pwdError.value = humanizeError(e)
  } finally {
    settingPwd.value = false
  }
}

async function confirmUnlock() {
  unlocking.value = true
  try {
    const realmId = (account.value as any)?.realm_id ?? (viewPermission.value as any)?.realm_id
    await embyApi.unlockView(realmId)
    toast.success('解锁成功')
    showUnlockConfirm.value = false
    await refreshAccount()
  } catch (e) {
    toast.error(humanizeError(e))
  } finally {
    unlocking.value = false
  }
}

function openScheme(url: string) {
  let finalUrl = url
  // #758：一键导入时可临时带上播放密码（只放内存，不存）
  if (importPassword.value) {
    try {
      const u = new URL(url)
      // 常见 scheme 参数名兼容：password / pwd
      if (!u.searchParams.has('password') && !u.searchParams.has('pwd')) {
        u.searchParams.set('password', importPassword.value)
      }
      finalUrl = u.toString()
    } catch {
      finalUrl = url
    }
  }
  window.location.href = finalUrl
}

function daysLeft(endDate?: string | null): number {
  if (!endDate) return 0
  const diff = new Date(endDate).getTime() - Date.now()
  return Math.max(0, Math.ceil(diff / 86400000))
}

onMounted(() => {
  void refreshAccount()
})
</script>

<style scoped>
/* 接线员：v4 纸片风 */
.op-rows {
  background: var(--paper-card);
  border: 1.5px solid var(--line);
  border-radius: 12px;
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
  width: 32px;
  height: 32px;
  border-radius: 10px;
  border: 1.5px solid var(--line);
  background: var(--paper-card);
  color: var(--ink2);
  cursor: pointer;
}
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
  padding: 6px 4px;
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
  border-radius: 12px;
  padding: 12px 14px;
}
.realm-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
}
.realm-head strong { flex: 1; }
.badge {
  font-size: 11px;
  font-weight: 800;
  border-radius: 10px;
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

/* 一键导入 */
.import-pwd-input {
  width: 100%;
  box-sizing: border-box;
  font-size: 14px;
  padding: 10px 14px;
  margin-bottom: 10px;
  border: 1.5px solid var(--line);
  border-radius: 10px;
  background: var(--paper-card);
  color: var(--ink);
}
.import-pwd-input:focus-visible { outline: 2px solid var(--red); outline-offset: 1px; }
.scheme-btns { display: flex; flex-wrap: wrap; gap: 10px; }
.scheme-btn {
  font-size: 14px;
  font-weight: 800;
  padding: 10px 18px;
  border-radius: 12px;
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
  border-radius: 50%;
  background: var(--red);
  color: #fff;
  font-size: 14px;
  font-weight: 800;
  display: flex;
  align-items: center;
  justify-content: center;
}
.guide-step b { font-size: 14px; }
.guide-step p { font-size: 12px; color: var(--ink2); margin: 4px 0 0; line-height: 1.7; }

/* 按钮 */
.op-btn {
  font-size: 15px;
  font-weight: 800;
  letter-spacing: 2px;
  padding: 12px 28px;
  border-radius: 12px;
  cursor: pointer;
  border: 2px solid var(--ink);
}
.op-btn.primary {
  background: var(--red);
  border-color: var(--red);
  color: #fff;
  box-shadow: 0 3px 0 #a33319;
}
.op-btn.primary:active { transform: translateY(2px); box-shadow: none; }
.op-btn.primary:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }
.op-btn.ghost { background: transparent; color: var(--ink2); }
.op-btn:focus-visible { outline: 2px solid var(--red); outline-offset: 2px; }

/* 弹窗 */
.modal-mask {
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
  border-radius: 16px;
  padding: 22px 20px;
  box-shadow: 4px 4px 0 rgba(0, 0, 0, 0.15);
}
.modal-title { font-size: 17px; font-weight: 800; margin: 0 0 8px; letter-spacing: 1px; }
.modal-desc { font-size: 13px; color: var(--ink2); line-height: 1.7; margin: 0 0 14px; }
.modal-desc strong { color: var(--red); }
.modal-input {
  width: 100%;
  box-sizing: border-box;
  font-size: 15px;
  padding: 12px 14px;
  border: 1.5px solid var(--line);
  border-radius: 10px;
  background: #fff;
  color: var(--ink);
  margin-bottom: 6px;
}
.modal-input:focus-visible { outline: 2px solid var(--red); outline-offset: 1px; }
.modal-error { font-size: 12px; color: var(--red); margin: 4px 0 0; }
.modal-actions { display: flex; gap: 10px; margin-top: 14px; }
.modal-actions .op-btn { flex: 1; }

.free-note p { font-size: 13px; color: var(--ink2); line-height: 1.8; margin: 10px 0 0; }
</style>
