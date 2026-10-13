<template>
  <div class="v4-scope">
    <div class="v4-page">
      <!-- 日志头 -->
      <header class="v4-loghead">
        <span class="v4-stamp">营地一角 · 老向导</span>
        <h1>🧭 向导</h1>
        <p>营地里坐着位老向导，片库的事，问他准没错。</p>
        <div v-if="available" class="v4-fire">🟢 {{ quotaText }}</div>
        <div v-else-if="loading" class="v4-skeleton" style="height:32px;width:60%;margin-top:10px"></div>
      </header>

      <!-- 装备调试：第三方播放器要的三件套 -->
      <section class="v4-sec">
        <div class="v4-sec-title">🧭 装备调试</div>
        <p class="v4-sec-sub">在第三方播放器里填这三项，就能开始探险</p>
        <div class="v4-card">
          <div v-if="equipLoading" class="v4-skeleton" style="height:110px"></div>
          <div v-else-if="accountError && !account">
            <div class="v4-err">装备清单拿不到，请检查网络后重试</div>
            <button class="v4-retry" type="button" @click="refreshEquip">重新加载</button>
          </div>
          <template v-else-if="viewGranted">
            <div class="equip-row">
              <span class="equip-label">服务器</span>
              <span class="equip-value mono">{{ serverUrl }}</span>
              <button class="equip-btn" type="button" title="复制" @click="copyText(serverUrl, 'server')">
                <Check v-if="copiedField === 'server'" :size="14" class="ok" />
                <Copy v-else :size="14" />
              </button>
            </div>
            <div class="equip-row">
              <span class="equip-label">用户名</span>
              <span class="equip-value mono">{{ embyUsername }}</span>
              <button class="equip-btn" type="button" title="复制" @click="copyText(embyUsername, 'user')">
                <Check v-if="copiedField === 'user'" :size="14" class="ok" />
                <Copy v-else :size="14" />
              </button>
            </div>
            <div class="equip-row">
              <span class="equip-label">播放密码</span>
              <span class="equip-value mono">
                <template v-if="hasPlayPassword">{{ showPlayPassword ? '已设置' : '••••••••' }}</template>
                <template v-else>未设置</template>
              </span>
              <button v-if="hasPlayPassword" class="equip-btn" type="button" :title="showPlayPassword ? '隐藏' : '查看'" @click="showPlayPassword = !showPlayPassword">
                <EyeOff v-if="showPlayPassword" :size="14" />
                <Eye v-else :size="14" />
              </button>
              <button class="equip-text-btn" type="button" @click="showSetPlayPwd = true">
                {{ hasPlayPassword ? '修改' : '设置' }}
              </button>
            </div>
            <p class="equip-tip">播放密码只用来在第三方播放器里登录，跟门户登录密码是两回事。</p>
          </template>
          <div v-else-if="viewPermission" class="v4-err">
            <template v-if="viewPermission.realm_free">
              <b>装备清单被锁起来了。</b><br>
              花 <b>{{ viewPermission.unlock_points }}</b> 积分解锁查看权限<span v-if="viewPermission.unlock_days > 0">，有效期 {{ viewPermission.unlock_days }} 天</span><span v-else>，永久有效</span>。你现在有 <b>{{ viewPermission.points_balance }}</b> 积分。
              <br>
              <button
                class="v4-retry"
                type="button"
                :disabled="unlocking || viewPermission.points_balance < viewPermission.unlock_points"
                @click="showUnlockConfirm = true"
              >
                {{ unlocking ? '解锁中…' : viewPermission.points_balance < viewPermission.unlock_points ? '积分不够' : `花 ${viewPermission.unlock_points} 积分解锁` }}
              </button>
            </template>
            <template v-else>
              <b>装备清单被锁起来了。</b><br>开通会员后可见。
            </template>
          </div>
          <div v-else class="v4-empty">暂无装备信息</div>
        </div>
      </section>

      <div v-if="loading" class="v4-sec"><div class="v4-skeleton" style="height:180px"></div></div>

      <!-- 向导没在：说清楚是哪种情况 -->
      <section v-else-if="!available" class="v4-sec">
        <div class="guide-card">
          <div class="avatar">🧙‍♂️</div>
          <div class="who">
            <div class="nm">老陈 <span class="tag">今天没出摊</span></div>
            <div class="motto">向导暂时不在营地。</div>
          </div>
        </div>
        <div class="v4-err" style="margin-top:12px">
          <template v-if="netError">网络开小差了，请检查网络后重试。</template>
          <template v-else>{{ status?.reason || '管理员还没请向导来坐镇。' }}</template><br>
          <span class="tip">着急的话可以去 <b>求救信号</b> 发封求救信，会有人回你。</span>
        </div>
        <button class="v4-retry" style="margin-top:12px" :disabled="loading" @click="load">再去看看</button>
      </section>

      <template v-else>
        <!-- 向导名片 -->
        <section class="v4-sec">
          <div class="guide-card">
            <div class="avatar">🧙‍♂️</div>
            <div class="who">
              <div class="nm">老陈 <span class="tag">营地向导 · 在线</span></div>
              <div class="motto">"问我，包打听。"</div>
            </div>
            <span class="dot"></span>
          </div>
        </section>

        <!-- 快捷问 -->
        <section class="v4-sec">
          <div class="v4-sec-title">💬 直接问他</div>
          <div class="chips">
            <button
              v-for="s in SUGGESTIONS"
              :key="s"
              class="chip"
              type="button"
              :disabled="sending"
              @click="ask(s)"
            >{{ s }}</button>
          </div>
        </section>

        <!-- 聊天 -->
        <section class="v4-sec">
          <div ref="scroller" class="chat">
            <div v-if="!bubbles.length" class="chat-empty">
              <p>老陈刚沏上茶，坐下聊两句？</p>
            </div>
            <div v-for="(b, i) in bubbles" :key="i" class="msg" :class="b.role">
              <div v-if="b.role === 'guide'" class="who">老陈 · 向导</div>
              <p>{{ b.content }}</p>
            </div>
            <div v-if="sending" class="msg guide">
              <div class="who">老陈 · 向导</div>
              <span class="typing" aria-label="向导正在思考"><i></i><i></i><i></i></span>
            </div>
          </div>
        </section>

        <!-- 输入 -->
        <div class="inputbar">
          <input
            v-model="question"
            type="text"
            :disabled="sending || exhausted"
            :placeholder="exhausted ? '今天问够了，明天再来' : '问问老陈…'"
            @keyup.enter="ask()"
          >
          <button :disabled="sending || exhausted || !question.trim()" @click="ask()">
            {{ sending ? '想着…' : '发送' }}
          </button>
        </div>
      </template>
    </div>

    <!-- 设置播放密码 -->
    <div v-if="showSetPlayPwd" class="v4-modal-mask" @click.self="showSetPlayPwd = false">
      <div class="v4-modal" role="dialog" aria-modal="true" aria-label="设置播放密码">
        <h3 class="v4-modal-title">设置播放密码</h3>
        <p class="v4-modal-desc">这个密码填在第三方播放器（Infuse、SenPlayer 等）的登录里</p>
        <input v-model="playPwd" type="password" class="v4-input" autocomplete="new-password" placeholder="至少 6 位" />
        <div class="v4-modal-actions">
          <button class="v4-btn-ghost" type="button" @click="showSetPlayPwd = false">取消</button>
          <button class="v4-btn-primary" type="button" :disabled="playPwdLoading || !playPwd" @click="handleSetPlayPassword">
            {{ playPwdLoading ? '提交中…' : '确认设置' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 解锁确认 -->
    <div v-if="showUnlockConfirm" class="v4-modal-mask" @click.self="showUnlockConfirm = false">
      <div class="v4-modal" role="dialog" aria-modal="true" aria-label="解锁装备清单">
        <h3 class="v4-modal-title">解锁装备清单</h3>
        <p class="v4-modal-desc">花 {{ viewPermission?.unlock_points }} 积分，解锁服务器地址、用户名与播放密码的查看权限。</p>
        <div class="v4-modal-actions">
          <button class="v4-btn-ghost" type="button" @click="showUnlockConfirm = false">取消</button>
          <button class="v4-btn-primary" type="button" :disabled="unlocking" @click="handleUnlockView">
            {{ unlocking ? '解锁中…' : '确认解锁' }}
          </button>
        </div>
      </div>
    </div>

    <PanelTabbar />
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import PanelTabbar from '@/components/panel/PanelTabbar.vue'
import { aiApi, embyApi, type AiStatus, type AccountCard } from '@/api'
import { useUserStore } from '@/stores/user'
import { useToast } from '@/composables/useToast'
import { humanizeError } from '@/composables/usePanelApi'
import { toastCopy } from '@/constants/toastMessages'
import { toUserMessage } from '@/utils/toUserMessage'
import { useClipboard } from '@/composables/useClipboard'
import { Copy, Check, Eye, EyeOff } from 'lucide-vue-next'

interface Bubble {
  role: 'me' | 'guide' | 'error'
  content: string
}

const toast = useToast()
const userStore = useUserStore()
const { copy } = useClipboard()

const loading = ref(true)
const status = ref<AiStatus | null>(null)
const question = ref('')
const sending = ref(false)
const bubbles = ref<Bubble[]>([])
const MAX_BUBBLES = 80
function trimBubbles() {
  if (bubbles.value.length > MAX_BUBBLES) bubbles.value.splice(0, bubbles.value.length - MAX_BUBBLES)
}
const scroller = ref<HTMLElement | null>(null)
/** true=请求抛错（网络开小差）；false=明确未启用（管理员还没请向导） */
const netError = ref(false)

/* ===== 装备调试（Emby 账号三件套，逻辑与 /profile 的 Emby 账号卡一致） ===== */
const equipLoading = ref(true)
const account = ref<AccountCard | null>(null)
const accountError = ref(false)
const copiedField = ref('')
const showPlayPassword = ref(false)
const showSetPlayPwd = ref(false)
const playPwd = ref('')
const playPwdLoading = ref(false)
const unlocking = ref(false)
const showUnlockConfirm = ref(false)

const embyUsername = computed(
  () => account.value?.emby_username || (userStore.user as any)?.emby_username || (userStore.user as any)?.username || '—',
)
const serverUrl = computed(() => account.value?.base_url || window.location.origin)
const hasPlayPassword = computed(() => !!account.value?.has_password)
// 查看权限：服务器地址 / 用户名 / 密码只给有权限的人看，没权限时后端也不下发
const viewPermission = computed(() => account.value?.view_permission || null)
const viewGranted = computed(() => !!viewPermission.value?.granted)

async function loadEquip() {
  equipLoading.value = true
  accountError.value = false
  try {
    account.value = await embyApi.getAccountCard()
  } catch {
    if (!account.value) accountError.value = true
  } finally {
    equipLoading.value = false
  }
}
function refreshEquip() {
  loadEquip()
}

const COPY_LABELS: Record<string, string> = {
  server: '服务器地址',
  user: '用户名',
}
let copyTimer: number | undefined
async function copyText(text: string, field: string) {
  const ok = await copy(text, COPY_LABELS[field] || '服务器地址')
  if (!ok) return
  copiedField.value = field
  window.clearTimeout(copyTimer)
  copyTimer = window.setTimeout(() => {
    if (copiedField.value === field) copiedField.value = ''
  }, 1600)
}

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
    try {
      account.value = await embyApi.getAccountCard()
    } catch {
      // 密码已设置成功，刷新失败只静默，不翻转成功提示
    }
  } catch (err: any) {
    toast.error(toUserMessage(err, 'settingFailed'))
  } finally {
    playPwdLoading.value = false
  }
}

/** 向导是否在岗（能力来自 aiApi.status，与原 AI 助手页一致） */
const available = computed(() => status.value?.enabled === true)
const quotaText = computed(() => {
  const s = status.value
  if (!s?.enabled) return ''
  if (!s.daily_limit || s.remaining === null) return '随便问，不限次数'
  return `今天还能问 ${s.remaining} 次`
})
const exhausted = computed(() => available.value && status.value?.remaining === 0)

/** 设计稿 game-guide.html 的快捷问 */
const SUGGESTIONS = [
  '今天有啥新片？',
  '我的能量够换执照吗？',
  '许愿池里我的愿望咋样了？',
]

async function load() {
  loading.value = true
  netError.value = false
  try {
    status.value = await aiApi.status()
  } catch {
    status.value = null
    netError.value = true
  } finally {
    loading.value = false
  }
}

async function scrollToEnd() {
  await nextTick()
  if (scroller.value) {
    scroller.value.scrollTo({
      top: scroller.value.scrollHeight,
      behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
    })
  }
}

async function ask(text?: string) {
  const content = (text ?? question.value).trim()
  if (!content || sending.value) return
  question.value = ''
  bubbles.value.push({ role: 'me', content })
  trimBubbles()
  sending.value = true
  scrollToEnd()
  try {
    // 只把 me / guide 轮次交给后端（它也会再过滤一次）
    const history = bubbles.value
      .filter((b) => b.role !== 'error')
      .map((b): { role: 'user' | 'assistant'; content: string } => ({
        role: b.role === 'me' ? 'user' : 'assistant',
        content: b.content,
      }))
      .slice(-8)
    const res = await aiApi.ask(content, history)
    bubbles.value.push({ role: 'guide', content: res.answer })
    trimBubbles()
    status.value = { ...(status.value as AiStatus), ...res }
  } catch (err) {
    bubbles.value.push({ role: 'error', content: humanizeError(err) })
    trimBubbles()
    toast.error(humanizeError(err))
    // 配额用尽 / 被关闭时刷新一次状态，界面提示随之更新
    load()
  } finally {
    sending.value = false
    scrollToEnd()
  }
}

onMounted(() => {
  load()
  loadEquip()
  bubbles.value.push({
    role: 'guide',
    content: '哟，来了。老陈我在这营地坐了三年，片库里哪部片子后劲大、哪条线路最稳，门儿清。甭客气，随便问。',
  })
})
onUnmounted(() => window.clearTimeout(copyTimer))
</script>

<style scoped>
/* 向导名片：设计稿 game-guide.html 原值 */
.guide-card {
  display: flex;
  align-items: center;
  gap: 12px;
  background: #FFFDF6;
  border: 1.5px solid var(--line);
  border-radius: 12px;
  padding: 14px 16px;
  position: relative;
}
.guide-card .avatar { font-size: 40px; flex: none; }
.guide-card .who { flex: 1; min-width: 0; }
.guide-card .nm { font-size: 15px; font-weight: 800; }
.guide-card .tag {
  font-size: 11px;
  font-weight: 400;
  color: var(--green);
  background: #F1F4EC;
  border: 1px solid var(--green);
  border-radius: 20px;
  padding: 2px 8px;
  margin-left: 6px;
  white-space: nowrap;
}
.guide-card .motto { font-size: 12px; color: var(--ink2); margin-top: 4px; font-style: italic; }
.guide-card .dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--green);
  flex: none;
}

/* ===== 装备调试 ===== */
.equip-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 0;
  border-bottom: 1px dashed var(--line);
  font-size: 13px;
}
.equip-row:last-of-type { border-bottom: none; }
.equip-label {
  flex: none;
  width: 64px;
  color: var(--ink2);
  font-weight: 700;
}
.equip-value {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--ink);
}
.equip-value.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
}
.equip-btn {
  flex: none;
  border: 1px solid var(--line);
  background: #FFFDF6;
  border-radius: 8px;
  width: 32px;
  height: 32px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--ink2);
  cursor: pointer;
  min-height: 32px;
}
.equip-btn .ok { color: var(--green); }
.equip-text-btn {
  flex: none;
  border: none;
  background: none;
  color: var(--red);
  font-size: 13px;
  font-weight: 700;
  cursor: pointer;
  min-height: 44px;
  padding: 0 4px;
}
.equip-tip {
  font-size: 12px;
  color: var(--ink2);
  margin: 10px 0 0;
  line-height: 1.6;
}

/* v4 弹窗 */
.v4-modal-mask {
  position: fixed;
  inset: 0;
  z-index: 60;
  background: rgba(42, 38, 32, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
}
.v4-modal {
  background: #FFFDF6;
  border: 1.5px solid var(--ink);
  border-radius: 12px;
  box-shadow: 4px 4px 0 rgba(42, 38, 32, 0.2);
  padding: 20px;
  width: 100%;
  max-width: 340px;
}
.v4-modal-title { font-size: 16px; font-weight: 800; margin: 0 0 6px; color: var(--ink); }
.v4-modal-desc { font-size: 13px; color: var(--ink2); margin: 0 0 14px; line-height: 1.6; }
.v4-input {
  width: 100%;
  box-sizing: border-box;
  font-size: 14px;
  padding: 12px 14px;
  border: 1.5px solid var(--line);
  border-radius: 10px;
  background: #FFFDF6;
  color: var(--ink);
  margin-bottom: 14px;
}
.v4-modal-actions { display: flex; gap: 10px; justify-content: flex-end; }
.v4-btn-ghost {
  border: 1.5px solid var(--line);
  background: #FFFDF6;
  color: var(--ink);
  border-radius: 10px;
  padding: 10px 18px;
  font-size: 14px;
  font-weight: 700;
  cursor: pointer;
  min-height: 44px;
}
.v4-btn-primary {
  border: none;
  background: var(--red);
  color: #fff;
  border-radius: 10px;
  padding: 10px 18px;
  font-size: 14px;
  font-weight: 800;
  cursor: pointer;
  box-shadow: 0 3px 0 #a33319;
  min-height: 44px;
}
.v4-btn-primary:active { transform: translateY(2px); box-shadow: none; }
.v4-btn-primary:disabled, .v4-btn-ghost:disabled, .v4-retry:disabled { opacity: 0.5; cursor: default; }

/* 快捷问 chips */
.chips { display: flex; flex-wrap: wrap; gap: 8px; }
.chip {
  border: 1.5px solid var(--line);
  background: #FFFDF6;
  color: var(--ink);
  border-radius: 20px;
  padding: 8px 14px;
  font-size: 13px;
  min-height: 44px;
  cursor: pointer;
}
.chip:active { transform: scale(0.96); border-color: var(--red); }
.chip:disabled { opacity: 0.5; cursor: default; }

/* 聊天区 */
.chat {
  background: #FFFDF6;
  border: 1.5px solid var(--line);
  border-radius: 12px;
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: 200px;
  max-height: 52vh;
  overflow-y: auto;
}
.chat-empty { color: var(--ink2); font-size: 13px; text-align: center; padding: 30px 0; }
.chat-empty p { margin: 0; }
.msg { max-width: 82%; }
.msg p { margin: 0; font-size: 13.5px; line-height: 1.7; white-space: pre-wrap; }
.msg.guide {
  align-self: flex-start;
  background: var(--paper2);
  border: 1px solid var(--line);
  border-radius: 4px 12px 12px 12px;
  padding: 10px 13px;
}
.msg.me {
  align-self: flex-end;
  background: #FBE9E2;
  border: 1px solid var(--red);
  border-radius: 12px 4px 12px 12px;
  padding: 10px 13px;
}
.msg.error {
  align-self: stretch;
  background: #FBE9E2;
  border: 1px dashed var(--red);
  border-radius: 10px;
  padding: 10px 13px;
  color: var(--red);
  font-size: 13px;
}
.msg .who { font-size: 11px; color: var(--ink2); margin-bottom: 4px; }

/* 思考中省略号：只用 opacity（60fps） */
.typing { display: inline-flex; gap: 5px; padding: 6px 0; }
.typing i {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--ink2);
  animation: blink 1.2s infinite;
}
.typing i:nth-child(2) { animation-delay: 0.2s; }
.typing i:nth-child(3) { animation-delay: 0.4s; }
@keyframes blink { 0%, 100% { opacity: 0.25; } 50% { opacity: 1; } }

/* 输入条 */
.inputbar {
  display: flex;
  gap: 8px;
  margin: 14px 20px 0;
}
.inputbar input {
  flex: 1;
  min-width: 0;
  font-size: 16px;
  padding: 12px 14px;
  border: 1.5px solid var(--line);
  border-radius: 10px;
  background: #FFFDF6;
  color: var(--ink);
}
.inputbar button {
  flex-shrink: 0;
  background: var(--red);
  color: #fff;
  border: none;
  font-size: 14px;
  font-weight: 800;
  padding: 0 18px;
  border-radius: 10px;
  box-shadow: 0 3px 0 #a33319;
  cursor: pointer;
}
.inputbar button:active { transform: translateY(2px); box-shadow: none; }
.inputbar button:disabled { background: var(--ink2); box-shadow: none; cursor: default; }

.tip { font-size: 12px; color: var(--ink2); }
.tip b { color: var(--gold); }

.chip:focus-visible,
.v4-retry:focus-visible,
.v4-btn-primary:focus-visible,
.v4-btn-ghost:focus-visible,
.equip-btn:focus-visible,
.equip-text-btn:focus-visible,
.v4-input:focus-visible,
.inputbar input:focus-visible,
.inputbar button:focus-visible {
  outline: 2px solid var(--red);
  outline-offset: 2px;
}
</style>
