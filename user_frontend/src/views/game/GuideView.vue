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
              <span class="typing" role="status" aria-label="向导正在思考"><i></i><i></i><i></i></span>
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
            {{ sending ? '想着' : '发送' }}
          </button>
        </div>
      </template>
    </div>

    <PanelTabbar />
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import PanelTabbar from '@/components/panel/PanelTabbar.vue'
import { aiApi, type AiStatus } from '@/api'
import { humanizeError } from '@/composables/usePanelApi'

interface Bubble {
  role: 'me' | 'guide' | 'error'
  content: string
}

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
    // 配额用尽 / 被关闭时刷新一次状态，界面提示随之更新
    load()
  } finally {
    sending.value = false
    scrollToEnd()
  }
}

onMounted(() => {
  load()
  bubbles.value.push({
    role: 'guide',
    content: '哟，来了。老陈我在这营地坐了三年，片库里哪部片子后劲大、哪条线路最稳，门儿清。甭客气，随便问。',
  })
})
</script>

<style scoped>
/* 向导名片：设计稿 game-guide.html 原值 */
.guide-card {
  display: flex;
  align-items: center;
  gap: 12px;
  background: var(--paper-card);
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

.v4-retry:disabled { opacity: 0.5; cursor: default; }

/* 快捷问 chips */
.chips { display: flex; flex-wrap: wrap; gap: 8px; }
.chip {
  border: 1.5px solid var(--line);
  background: var(--paper-card);
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
  background: var(--paper-card);
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
  background: var(--paper-card);
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
.inputbar input:focus-visible,
.inputbar button:focus-visible {
  outline: 2px solid var(--red);
  outline-offset: 2px;
}

/* 减弱动效：按下不再位移 */
@media (prefers-reduced-motion: reduce) {
  .chip:active, .inputbar button:active { transform: none; }
}
</style>
