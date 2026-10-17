<template>
  <div class="v4-scope">
    <div class="v4-page">
      <!-- 日志头 -->
      <header class="v4-loghead">
        <span class="v4-stamp">{{ facilityStamp('signal') }}</span>
        <h1><span aria-hidden="true">📡</span> 求救信号</h1>
        <p>探险途中遇险？摇响信号机，营地马上派救援队。</p>
      </header>

      <!-- 信号机 -->
      <section class="v4-sec sig-sec" aria-hidden="true">
        <div class="signal-machine">
          <div class="tower">📡</div>
          <div class="waves"><span></span><span></span><span></span></div>
          <div class="morse-deco">· · · − − − · · ·<small>SOS · 国际求救信号</small></div>
        </div>
      </section>

      <!-- 发信号 -->
      <section class="v4-sec">
        <div class="v4-sec-title"><span aria-hidden="true">⚠️</span> 遇险类型</div>
        <div class="v4-sec-sub">先选一种，救援队好带对工具</div>
        <div
          class="types"
          role="radiogroup"
          aria-label="遇险类型"
          @keydown="(e) => {
            const keys = ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'];
            if (!keys.includes(e.key)) return;
            e.preventDefault();
            const step = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : -1;
            const items = Array.from((e.currentTarget as HTMLElement).children) as HTMLButtonElement[];
            const i = items.indexOf(e.target as HTMLButtonElement);
            const n = (i + step + items.length) % items.length;
            form.category = categoryOptions[n].value;
            items[n]?.focus();
          }"
        >
          <button
            v-for="c in categoryOptions"
            :key="c.value"
            class="type"
            :class="{ on: form.category === c.value }"
            role="radio"
            :aria-checked="form.category === c.value"
            :tabindex="(form.category || categoryOptions[0]?.value) === c.value ? 0 : -1"
            @click="form.category = c.value"
          >
            <span class="ic" aria-hidden="true">{{ c.icon }}</span>
            <span class="lbl">{{ c.label }}</span>
          </button>
        </div>

        <div class="v4-sec-title"><span aria-hidden="true">✉️</span> 一句话说清险情</div>
        <div class="v4-sec-sub">让救援队一眼看懂哪里遇了险</div>
        <input
          v-model="form.title"
          class="field"
          type="text"
          placeholder="比如：《一念永恒》第 3 集播放卡住"
          aria-label="一句话说清险情"
          maxlength="100"
        />

        <div class="v4-sec-title"><span aria-hidden="true">✉️</span> 求救信</div>
        <div class="v4-sec-sub">写清楚在哪儿遇的险，发生了什么</div>
        <textarea
          v-model="form.message"
          placeholder="比如：《一念永恒》看到第 3 集，转圈半天出不来…"
          aria-label="求救信"
          maxlength="1000"
        ></textarea>

        <button class="send-btn" :disabled="sending" @click="sendSOS">
          <span v-if="!sending" aria-hidden="true">📡</span>{{ sending ? '信号发出中' : ' 发出求救信号' }}
        </button>

        <!-- 摩斯电码发送动画 -->
        <div v-if="morseShow" class="morse-panel" role="status" aria-live="polite">
          <div class="morse-title"><span aria-hidden="true">📡</span> 正在发送摩斯电码…</div>
          <div class="morse-code">
            <div
              v-for="(s, i) in morseSyms"
              :key="i"
              class="sym"
              :class="{ lit: s.lit, gap: s.gap }"
            >{{ s.ch }}</div>
          </div>
          <div class="morse-status">{{ morseStatus }}</div>
        </div>
      </section>

      <!-- 事故记录 -->
      <section class="v4-sec">
        <div class="v4-sec-title"><span aria-hidden="true">📋</span> 探险事故记录</div>
        <div class="v4-sec-sub">你发过的求救信号，点开看救援进度</div>

        <div v-if="loading">
          <div v-for="i in 3" :key="i" class="v4-skeleton" style="height:76px;margin-bottom:10px"></div>
        </div>
        <div v-else-if="loadError" class="v4-err">
          事故记录读取失败：{{ loadError }}<br>
          <button class="v4-retry" @click="fetchTickets()">重试</button>
        </div>
        <div v-else-if="tickets.length === 0" class="v4-empty">
          营地一切安好，还没有发过求救信号 📡
        </div>

        <div v-else class="cards">
          <div
            v-for="t in tickets"
            :key="t.id"
            class="card"
            :class="{ open: expandedId === t.id }"
          >
            <button class="card-head" @click="toggleDetail(t)" :aria-expanded="expandedId === t.id">
              <span class="card-title"><span aria-hidden="true">{{ categoryIcon(t.category) }}</span> {{ t.title }}</span>
              <span class="chip" :class="statusChip(t.status)"><span v-if="statusIcon(t.status)" aria-hidden="true">{{ statusIcon(t.status) }} </span>{{ statusLabel(t.status) }}</span>
            </button>
            <div class="card-meta">{{ categoryLabel(t.category) }} · 最近更新 {{ formatDate(t.updated_at) }}</div>

            <div v-if="expandedId === t.id" class="card-detail">
              <div v-if="messagesLoading">
                <div v-for="i in 3" :key="i" class="v4-skeleton" style="height:56px;margin-bottom:8px"></div>
              </div>
              <div v-else-if="messagesError" class="v4-err">对话加载失败：{{ /network|failed to fetch|fetch failed|offline|断网/i.test(String(messagesError)) ? '网络好像断了，检查一下再重试' : /timeout|timed out|超时/i.test(String(messagesError)) ? '营地那边没回应，稍后再试' : '营地那边出了点状况，稍后再试' }}<br><button class="v4-retry" @click="() => selectedTicket && fetchMessages(selectedTicket.id)">重试</button></div>
              <div v-else ref="messagesEl" class="messages-list" tabindex="0" role="log" aria-label="对话记录">
                <p v-if="!messages.length" class="messages-empty">还没有对话内容，营地马上就来认领</p>
                <div
                  v-for="msg in messages"
                  :key="msg.id"
                  class="message-item"
                  :class="{ 'message-camp': msg.is_admin }"
                >
                  <div class="message-content">
                    <span v-if="msg.is_admin" class="message-author">🏕️ 营地回复{{ msg.admin_name ? ` · ${msg.admin_name}` : '' }}</span>
                    <span v-else class="message-author me">🧭 我的求救信</span>
                    <span class="message-text">{{ msg.message }}</span>
                    <span class="message-time">{{ formatDate(msg.created_at) }}</span>
                  </div>
                </div>
              </div>

              <div v-if="t.status !== 'closed'" class="card-actions">
                <div class="reply-box">
                  <input
                    v-model="replyMessage"
                    type="text"
                    maxlength="2000"
                    placeholder="给营地回句话…"
                    aria-label="回复营地"
                    @keyup.enter="(e) => !e.isComposing && replyTicket()"
                  />
                  <button
                    class="btn-send"
                    :disabled="replying || !replyMessage.trim()"
                    @click="replyTicket"
                    :title="replying ? '发送中' : '发送回复'"
                    :aria-label="replying ? '发送中' : '发送回复'"
                  >➤</button>
                </div>
                <button class="close-record" @click="closeTicket"><span aria-hidden="true">🏁</span> 关闭记录</button>
              </div>
              <p v-else class="closed-hint">这条记录已关闭 · 感谢你的信任，继续探险吧 🧭</p>
            </div>
          </div>
        </div>
      </section>
    </div>

    <!-- R27：关闭记录二次确认（纸色风格，替代原生 confirm） -->
    <Modal v-model="showCloseConfirm" size="sm" content-class="paper-confirm">
      <template #header>
        <h3 class="modal-title">🏁 关闭记录</h3>
      </template>
      <p class="confirm-text">确定要标记脱险并关闭这条事故记录吗？关闭后不能再回复。</p>
      <template #footer>
        <div class="confirm-row">
          <button class="cbtn ghost" @click="showCloseConfirm = false">再想想</button>
          <button class="cbtn go" :disabled="closing" @click="doCloseTicket">{{ closing ? '关闭中' : '确定关闭' }}</button>
        </div>
      </template>
    </Modal>
  </div>
</template>

<script setup lang="ts">
/**
 * 求救信号 = 工单支持（游戏化）
 * 遇险类型宫格 → 求救信 → 摩斯电码发送动画 → 事故记录卡跟进
 */
import { ref, nextTick, onMounted, onUnmounted } from 'vue'
import Modal from '@/components/ui/Modal.vue'
import { facilityStamp } from '@/config/game'
import { ticketApi, type Ticket, type TicketMessage } from '@/api'
import { useToast } from '@/composables/useToast'
import { humanizeError } from '@/composables/usePanelApi'

const toast = useToast()

// 遇险类型 5 宫格：value 必须原样传给后端
const categoryOptions = [
  { value: 'emby', icon: '🎬', label: '播放卡住' },
  { value: 'account', icon: '👤', label: '账号问题' },
  { value: 'subscription', icon: '📜', label: '执照问题' },
  { value: 'payment', icon: '💰', label: '支付问题' },
  { value: 'other', icon: '❓', label: '其他险情' },
]

const statusConfig: Record<string, { chip: string; icon: string; label: string }> = {
  open: { chip: 'blue', icon: '📡', label: '已发出' },
  pending: { chip: 'gold', icon: '🛟', label: '救援中' },
  resolved: { chip: 'green', icon: '✅', label: '已脱险' },
  closed: { chip: 'gray', icon: '', label: '已关闭' },
}
function statusIcon(status: string) {
  return statusConfig[status]?.icon ?? ''
}

// 状态
const tickets = ref<Ticket[]>([])
const loading = ref(true)
/** 列表首屏拉取失败：和「真的没有求救信号」分开显示 */
const loadError = ref('')
const expandedId = ref<number | null>(null)
const selectedTicket = ref<Ticket | null>(null)
const messages = ref<TicketMessage[]>([])
const messagesLoading = ref(false)
/** 对话消息拉取失败：和「真的没有对话内容」分开显示 */
const messagesError = ref('')
const messagesEl = ref<HTMLElement | null>(null)
/** R53A-35：组件卸载标记 —— 卸载后迟到的列表响应不再写回状态 */
let ticketsViewAlive = true

// 发信号表单
const form = ref({ title: '', category: 'other', message: '' })
const sending = ref(false)

// 摩斯电码动画
const morseShow = ref(false)
const morseStatus = ref('')
const morseSyms = ref<{ ch: string; gap: boolean; lit: boolean }[]>([])
const morseTimers: number[] = []
/** R38 P2-1：摩斯动画的 resolve 句柄 —— 接口先失败时直接终结动画 Promise，
 *  避免清掉 timer 后 Promise 永远不 settle 导致 await 挂死 */
let morseAnimResolve: (() => void) | null = null

// 回复
const replyMessage = ref('')
const replying = ref(false)

function categoryIcon(category: string) {
  return categoryOptions.find(c => c.value === category)?.icon ?? '❓'
}
function categoryLabel(category: string) {
  return categoryOptions.find(c => c.value === category)?.label ?? '其他险情'
}
function statusChip(status: string) {
  return statusConfig[status]?.chip ?? 'gray'
}
function statusLabel(status: string) {
  const cfg = statusConfig[status]
  if (!cfg && import.meta.env.DEV) console.warn('[SignalView] 未识别的工单状态：', status)
  return cfg?.label ?? '处理中，稍后刷新查看'
}

/** 求救信号列表 */
// R41：静默模式 —— replyTicket / doCloseTicket / sendSOS 尾部刷新时 silent=true，不置 loading，
// 已展开的对话卡不再被 skeleton 卸载闪一次
async function fetchTickets(silent = false) {
  if (!silent) loading.value = true
  loadError.value = ''
  try {
    const res = await ticketApi.getMyTickets()
    if (!ticketsViewAlive) return
    tickets.value = (res as any) || []
  } catch (e) {
    if (!ticketsViewAlive) return
    if (!tickets.value.length) loadError.value = humanizeError(e)
    else toast.error(humanizeError(e))
  } finally {
    if (!silent) loading.value = false
  }
}

// SOS: ··· −−− ···（每符号点亮时长 + 间隔）
const MORSE_SEQ: [string, number][] = [
  ['·', 260], ['·', 260], ['·', 260],
  ['gap', 420],
  ['−', 560], ['−', 560], ['−', 560],
  ['gap', 420],
  ['·', 260], ['·', 260], ['·', 260],
]

const reducedMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches

function runMorseAnim(): Promise<void> {
  return new Promise((resolve) => {
    morseAnimResolve = resolve
    morseSyms.value = MORSE_SEQ.map(([ch]) => ({
      ch: ch === 'gap' ? '' : ch,
      gap: ch === 'gap',
      lit: false,
    }))
    morseStatus.value = '滴滴滴… 信号发出中'
    let i = 0
    const step = () => {
      if (i > 0) morseSyms.value[i - 1].lit = false
      if (i >= MORSE_SEQ.length) { morseAnimResolve = null; resolve(); return }
      const [ch, dur] = MORSE_SEQ[i]
      if (ch !== 'gap') morseSyms.value[i].lit = true
      i++
      morseTimers.push(window.setTimeout(step, dur))
    }
    step()
  })
}

/** 发出求救信号：摩斯动画（约 4 秒）与接口并发；接口先失败则立刻停动画弹错（R38 P2-1），成功才等动画播完显示"已发出" */
async function sendSOS() {
  const title = form.value.title.trim()
  const message = form.value.message.trim()
  if (!title || !message) {
    toast.error('先写清楚险情标题和求救信，再摇信号机')
    return
  }
  if (sending.value) return
  sending.value = true
  // R37 P3-2：减少动态偏好下跳过摩斯动画面板 —— reduce 用户看到的是 44px 空盒 + 空状态行，
  // 按钮「信号发出中」忙碌态已告知进度，不展示空面板
  const reduce = reducedMotion()
  if (!reduce) morseShow.value = true
  // R38 P2-1：接口先失败时立即停动画、清面板，错误不躲在 4 秒动画后面
  const createP = ticketApi.create({ title, category: form.value.category, message }).catch((e) => {
    morseTimers.forEach(clearTimeout)
    morseTimers.length = 0
    morseAnimResolve?.()
    morseAnimResolve = null
    morseShow.value = false
    throw e
  })
  try {
    if (!reduce) await runMorseAnim()
    await createP
    toast.success('求救信号已发出，营地马上派救援队')
    form.value = { title: '', category: 'other', message: '' }
    await fetchTickets(true)
    if (!reduce) {
      await new Promise(r => morseTimers.push(window.setTimeout(r, 1400)))
    }
  } catch (e) {
    toast.error(humanizeError(e))
  } finally {
    morseShow.value = false
    sending.value = false
  }
}

/** 事故记录卡展开/收起 */
let messagesSeq = 0
async function toggleDetail(t: Ticket) {
  if (expandedId.value === t.id) {
    expandedId.value = null
    selectedTicket.value = null
    return
  }
  expandedId.value = t.id
  selectedTicket.value = t
  messages.value = []
  messagesError.value = ''
  await fetchMessages(t.id)
}

/** 工单详情 + 对话消息 */
async function fetchMessages(ticketId: number) {
  const seq = ++messagesSeq
  messagesLoading.value = true
  messagesError.value = ''
  try {
    const res = await ticketApi.getMessages(ticketId)
    if (seq !== messagesSeq || expandedId.value !== ticketId) return
    messages.value = (res as any) || []
  } catch (e) {
    if (seq !== messagesSeq || expandedId.value !== ticketId) return
    messages.value = []
    messagesError.value = humanizeError(e)
  } finally {
    if (seq === messagesSeq) messagesLoading.value = false
  }
  if (seq === messagesSeq && expandedId.value === ticketId) {
    await scrollMessagesToBottom()
  }
}

/** 新消息滚到底部 */
async function scrollMessagesToBottom() {
  await nextTick()
  const el = messagesEl.value
  if (el) el.scrollTop = el.scrollHeight
}

/** 回复工单 */
async function replyTicket() {
  if (!replyMessage.value.trim() || !selectedTicket.value || replying.value) return
  replying.value = true
  try {
    await ticketApi.reply(selectedTicket.value.id, replyMessage.value.trim())
    replyMessage.value = ''
    await fetchMessages(selectedTicket.value.id)
    // R53A-34：回复已成功，列表刷新失败不能再弹通用错误（会被误解成「没发出去」）
    try {
      const res = await ticketApi.getMyTickets()
      tickets.value = (res as any) || []
    } catch {
      toast.error('消息已发出，列表刷新失败，下拉重试')
    }
  } catch (e) {
    toast.error(humanizeError(e))
  } finally {
    replying.value = false
  }
}

/** 关闭记录（标记脱险）：R27 —— 原生 confirm() 改用纸色 Modal 自定义确认弹窗 */
const showCloseConfirm = ref(false)
const closing = ref(false)
function closeTicket() {
  if (!selectedTicket.value) return
  showCloseConfirm.value = true
}
async function doCloseTicket() {
  if (!selectedTicket.value || closing.value) return
  closing.value = true
  try {
    await ticketApi.close(selectedTicket.value.id)
    toast.success('已标记脱险，记录关闭')
    expandedId.value = null
    selectedTicket.value = null
    await fetchTickets(true)
  } catch (e) {
    toast.error(humanizeError(e))
    // 失败时保持确认弹窗打开，用户可直接重试，不丢失上下文
    closing.value = false
    return
  }
  closing.value = false
  showCloseConfirm.value = false
}

/** 相对时间（与 TicketsView 同逻辑：服务器时钟超前时不显示负数） */
function formatDate(dateStr: string) {
  if (!dateStr) return ''
  const date = new Date(dateStr)
  // R41：非法日期不再原样吐 ISO 字符串 —— "时间未知"比 2026-13-99T99:99 像人话
  if (isNaN(date.getTime())) return '时间未知'
  const now = new Date()
  const diff = Math.max(0, now.getTime() - date.getTime())
  const days = Math.floor(diff / (1000 * 60 * 60 * 24))
  if (days === 0) {
    const hours = Math.floor(diff / (1000 * 60 * 60))
    if (hours === 0) {
      const minutes = Math.floor(diff / (1000 * 60))
      return minutes === 0 ? '刚刚' : `${minutes}分钟前`
    }
    return `${hours}小时前`
  }
  if (days === 1) return '昨天'
  if (days < 7) return `${days}天前`
  return date.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' })
}

onMounted(() => {
  fetchTickets()
})
onUnmounted(() => {
  // R53A-35：置失效标记，卸载后迟到的列表响应不再写回状态
  ticketsViewAlive = false
  morseTimers.forEach(t => window.clearTimeout(t))
  // R41：卸载时也终结摩斯动画 Promise，避免清掉 timer 后 Promise 永远不 settle（R38 P2-1 句柄）
  morseAnimResolve?.()
  morseAnimResolve = null
})
</script>

<style scoped>
/* 信号机：设计稿 game-ticket.html 原值 */
.sig-sec { padding-top: 6px; }
.signal-machine { text-align: center; padding: 8px 0 4px; position: relative; }
.tower {
  font-size: 64px;
  display: inline-block;
  animation: shake 2.4s ease-in-out infinite;
  filter: drop-shadow(0 6px 8px rgba(0,0,0,.2));
}
@keyframes shake { 0%,100% { transform: rotate(-4deg); } 50% { transform: rotate(4deg); } }
.waves { position: relative; height: 26px; margin-top: 2px; }
.waves span {
  position: absolute; left: 50%; top: 0;
  width: 14px; height: 14px; margin-left: -7px;
  border: 2px solid var(--red); border-radius: var(--paper-r-full, 50%);
  opacity: 0; animation: wave 2.4s infinite;
}
.waves span:nth-child(2) { animation-delay: .4s; }
.waves span:nth-child(3) { animation-delay: .8s; }
@keyframes wave { 0% { transform: scale(.6); opacity: .9; } 100% { transform: scale(4.2); opacity: 0; } }
.morse-deco {
  font-family: Georgia, serif; font-size: 20px; letter-spacing: 10px;
  color: var(--ink2); margin-top: 6px;
}
.morse-deco small { display: block; font-size: 10px; letter-spacing: 3px; margin-top: 4px; }

/* 遇险类型 5 宫格：设计稿 .type 原值，触达区补到 44px */
.types { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 16px; }
.type {
  background: var(--paper-card);
  border: 1.5px solid var(--line);
  border-radius: var(--paper-r-lg, 12px);
  padding: 14px 8px;
  min-height: 88px;
  text-align: center;
  cursor: pointer;
  transition: transform .12s ease, border-color .15s ease, background .15s ease;
  font-family: inherit;
  color: var(--ink);
}
.type .ic { font-size: 26px; display: block; margin-bottom: 6px; }
.type .lbl { font-size: 13px; font-weight: 700; }
.type.on { border-color: var(--red); background: #FBEDE7; box-shadow: 0 0 0 1.5px var(--red); }
.type:active { transform: scale(.95); }

/* 输入框：设计稿 textarea 原值 + 标题输入 */
.field {
  width: 100%;
  font-size: 16px;
  padding: 12px;
  border: 1.5px solid var(--line);
  border-radius: var(--paper-r-md, 10px);
  background: var(--paper-card);
  outline: none;
  color: var(--ink);
  font-family: inherit;
  margin-bottom: 16px;
}
.field:focus { border-color: var(--gold); }
textarea {
  width: 100%;
  font-size: 16px;
  padding: 12px;
  border: 1.5px solid var(--line);
  border-radius: var(--paper-r-md, 10px);
  background: var(--paper-card);
  min-height: 96px;
  resize: none;
  outline: none;
  color: var(--ink);
  font-family: inherit;
  line-height: 1.7;
}
textarea:focus { border-color: var(--gold); }
.v4-sec-title { margin-top: 4px; }
.v4-sec-title:first-child { margin-top: 0; }

/* 发送按钮：设计稿 .send-btn 原值 */
.send-btn {
  display: block;
  width: 100%;
  margin-top: 14px;
  background: var(--red);
  color: var(--paper-card);
  border: none;
  font-size: 16px;
  font-weight: 800;
  letter-spacing: 3px;
  padding: 15px;
  min-height: 44px;
  border-radius: 14px;
  cursor: pointer;
  box-shadow: 0 4px 0 var(--red-deep, #a33319);
  transition: transform .12s ease;
}
.send-btn:active:not(:disabled) { transform: translateY(3px); box-shadow: 0 1px 0 var(--red-deep, #a33319); }
.send-btn:disabled { background: var(--ink2); box-shadow: none; cursor: default; }

/* 摩斯电码发送动画：设计稿 .morse-panel 原值 */
.morse-panel {
  background: var(--ink);
  border-radius: 14px;
  padding: 20px;
  margin-top: 16px;
  color: #EDE6D6;
  text-align: center;
  animation: up .3s ease;
}
@keyframes up { from { transform: translateY(14px); opacity: 0; } }
.morse-title { font-size: 14px; font-weight: 800; letter-spacing: 2px; margin-bottom: 14px; }
.morse-code { display: flex; justify-content: center; gap: 8px; flex-wrap: wrap; margin-bottom: 14px; min-height: 44px; }
.sym {
  width: 38px; height: 38px;
  border-radius: var(--paper-r-sm, 8px);
  border: 1.5px solid #5a544a;
  display: flex; align-items: center; justify-content: center;
  font-size: 20px; font-weight: 800;
  color: #5a544a;
  transition: background .12s ease, border-color .12s ease, color .12s ease;
}
.sym.lit { background: var(--gold); border-color: var(--gold); color: var(--ink); box-shadow: 0 0 14px var(--gold); }
.sym.gap { border: none; width: 14px; }
.morse-status { font-size: 12px; color: var(--pulse-green, #9BE08A); letter-spacing: 1px; }

/* 事故记录卡：设计稿 .card 原值 + 展开详情 */
.cards { display: flex; flex-direction: column; gap: 10px; }
.card {
  background: var(--paper-card);
  border: 1.5px solid var(--line);
  border-radius: var(--paper-r-lg, 12px);
  padding: 13px 14px;
}
.card-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  width: 100%;
  min-height: 44px;
  margin: -6px -6px 0;
  padding: 6px 6px;
  background: none;
  border: none;
  cursor: pointer;
  text-align: left;
  font-family: inherit;
  color: var(--ink);
}
.card-title {
  font-size: 14px;
  font-weight: 700;
  flex: 1;
  word-break: break-word;
}
.chip {
  font-size: 11px; font-weight: 700;
  padding: 4px 10px;
  border-radius: var(--paper-r-2xl, 20px);
  white-space: nowrap;
}
.chip.blue { background: #E3ECF8; color: var(--blue); border: 1px solid var(--blue); }
.chip.gold { background: #F7EEDB; color: #7A5A18; border: 1px solid var(--gold); }
.chip.green { background: #E8F0E9; color: var(--green); border: 1px solid var(--green); }
.chip.gray { background: var(--paper2); color: var(--ink2); border: 1px solid var(--line); }
.card-meta { font-size: 11px; color: var(--ink2); margin-top: 4px; }

.card-detail {
  margin-top: 10px;
  border-top: 1px dotted var(--line);
  padding-top: 10px;
}

/* 对话 */
.messages-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 320px;
  overflow-y: auto;
}
.messages-empty {
  margin: 0; padding: 1.5rem 0;
  text-align: center;
  font-size: 12px;
  color: var(--ink2);
}
.message-item {
  padding: 10px 12px;
  border-radius: var(--paper-r-md, 10px);
  background: var(--paper2);
}
.message-item.message-camp { background: #E3ECF8; }
.message-content { display: flex; flex-direction: column; gap: 4px; }
.message-author { font-size: 12px; font-weight: 700; color: var(--blue); }
.message-author.me { color: var(--ink2); }
.message-text {
  color: var(--ink);
  font-size: 13px;
  line-height: 1.7;
  word-break: break-word;
  white-space: pre-wrap;
}
.message-time { font-size: 11px; color: var(--ink2); }

/* 回复 + 关闭 */
.card-actions { margin-top: 10px; display: flex; flex-direction: column; gap: 8px; }
.reply-box { display: flex; gap: 8px; }
.reply-box input {
  flex: 1;
  min-height: 44px;
  padding: 10px 12px;
  background: var(--paper-card);
  border: 1.5px solid var(--line);
  border-radius: var(--paper-r-md, 10px);
  color: var(--ink);
  font-size: 16px;
  font-family: inherit;
  outline: none;
}
.reply-box input:focus { border-color: var(--gold); }
.reply-box input:focus-visible { outline: 2px solid var(--gold); outline-offset: 2px; }
.btn-send {
  display: flex; align-items: center; justify-content: center;
  width: 44px; height: 44px;
  border-radius: var(--paper-r-md, 10px);
  border: none;
  background: var(--red);
  color: var(--paper-card);
  font-size: 18px;
  cursor: pointer;
  flex-shrink: 0;
  transition: transform .12s ease;
}
.btn-send:active:not(:disabled) { transform: scale(.93); }
.btn-send:disabled { opacity: .45; cursor: not-allowed; }
.close-record {
  min-height: 44px;
  border-radius: var(--paper-r-md, 10px);
  border: 1.5px solid var(--line);
  background: var(--paper2);
  color: var(--ink2);
  font-size: 13px;
  font-weight: 700;
  font-family: inherit;
  cursor: pointer;
}
.close-record:active { transform: scale(.98); }
.closed-hint { font-size: 12px; color: var(--ink2); margin: 10px 0 0; text-align: center; }

/* 键盘可达性：按钮获得焦点时给出可见轮廓 */
.type:focus-visible,
.send-btn:focus-visible,
.v4-retry:focus-visible,
.card-head:focus-visible,
.btn-send:focus-visible,
.close-record:focus-visible {
  outline: 2px solid var(--red);
  outline-offset: 2px;
}

/* R27：关闭确认弹窗 —— 注意：Modal 内容 Teleport 到 body，脱离 .v4-scope，
   纸色 token 在此解析不到，必须带字面量 fallback（值与 v4-paper.css .v4-scope 同步） */
.confirm-text {
  font-size: 14px;
  line-height: 1.7;
  color: var(--ink, #2A2620);
  margin: 0;
}
.confirm-row {
  display: flex;
  gap: 10px;
}
.cbtn {
  flex: 1;
  min-height: 44px;
  border-radius: var(--paper-r-md, 10px);
  font-size: 14px;
  font-weight: 700;
  cursor: pointer;
}
.cbtn.ghost {
  background: var(--paper-card, #FFFDF6);
  border: 1.5px solid var(--line, #D8CDB8);
  color: var(--ink2, #6B6257);
}
.cbtn.go {
  background: var(--red, #D9482B);
  border: none;
  color: var(--paper-card, #FFFDF6);
}
.cbtn:disabled { opacity: 0.5; cursor: wait; }

/* R27：减少动态偏好下装饰动画停掉（本地守卫） */
@media (prefers-reduced-motion: reduce) {
  .tower { animation: none; }
  .waves span { animation: none; }
  /* R45：摩斯面板 up .3s 入场动画也在减弱动态下关掉 */
  .morse-panel { animation: none; }
  .type:active, .send-btn:active:not(:disabled),
  .btn-send:active:not(:disabled), .close-record:active { transform: none; }
}
</style>

<style>
/* R27：Modal.vue 用 Teleport 挂到 body，scoped 样式够不到 —— 纸色覆盖写全局，
   全部挂在 .paper-confirm 命名空间下，不污染其他弹窗。
   注意：Modal 根是 fragment（两个 Teleport），class 写在 <Modal> 上会被 Vue 丢弃，
   必须走 contentClass prop 落到 .modal-container 上，本选择器才能命中。
   Teleport 后脱离 .v4-scope，纸色 token 解析不到，全部带字面量 fallback
   （值与 v4-paper.css .v4-scope 同步：--paper-card var(--paper-card, #FFFDF6) / --ink var(--ink, #2A2620) /
   --ink2 var(--ink2, #6B6257) / --line var(--line, #D8CDB8)） */
.paper-confirm .modal-content-wrapper {
  background: var(--paper-card, #FFFDF6);
  border: 1.5px solid var(--ink, #2A2620);
  border-radius: 14px;
  color: var(--ink, #2A2620);
}
.paper-confirm .modal-header {
  border-bottom: 1px solid var(--line, #D8CDB8);
  padding: 14px 18px;
}
.paper-confirm .modal-title {
  color: var(--ink, #2A2620);
  font-size: 16px;
  font-weight: 800;
}
.paper-confirm .modal-close { color: var(--ink2, #6B6257); }
.paper-confirm .modal-body { padding: 16px 18px; }
.paper-confirm .modal-footer {
  border-top: 1px solid var(--line, #D8CDB8);
  padding: 12px 18px;
}
</style>
