<template>
  <div class="v4-scope">
    <div class="v4-page">
      <!-- 日志头 -->
      <header class="v4-loghead">
        <span class="v4-stamp">{{ facilityStamp('pigeon') }}</span>
        <h1><span aria-hidden="true">🕊️</span> 信鸽</h1>
        <p>每天有信鸽飞来，脚上绑着信 —— 点一只鸽子，拆信。</p>
      </header>

      <!-- 栖木 -->
      <section class="v4-sec">
        <div class="v4-sec-title"><span aria-hidden="true">🪵</span> 鸽子栖木</div>
        <div class="v4-sec-sub">头上有红点的鸽子，信还没拆</div>
        <div class="perch">
          <div class="birds" aria-hidden="true">🕊️🕊️🕊️</div>
          <div class="bar" aria-hidden="true"></div>
          <p v-if="unreadCount > 0" class="perch-tip">
            <b>{{ unreadCount }}</b> 只鸽子带着信在栖木上等你
          </p>
          <p v-else class="perch-tip">信都拆完啦，鸽子们去歇着了 🕊️</p>
        </div>
      </section>

      <!-- 工具条 -->
      <section class="v4-sec">
        <div class="v4-sec-title"><span aria-hidden="true">📬</span> 今日来信</div>
        <div class="v4-sec-sub">点开鸽子 → 火漆拆信</div>
        <div class="toolbar">
          <div class="search-wrap">
            <span class="search-icon" aria-hidden="true">🔍</span>
            <input
              v-model="keyword"
              class="search-input"
              type="search"
              placeholder="搜信标题或内容…"
              aria-label="搜索信件"
            >
          </div>
          <button
            class="unread-toggle"
            :class="{ on: unreadOnly }"
            :aria-pressed="unreadOnly"
            @click="unreadOnly = !unreadOnly"
          >
            <span class="red-dot" aria-hidden="true"></span>
            只看未拆
          </button>
          <button
            class="refresh-btn"
            :class="{ 'is-loading': refreshing }"
            :disabled="refreshing || loading"
            :aria-label="refreshing ? '刷新中' : '刷新信件'"
            @click="onManualRefresh"
          >
            {{ refreshing ? '刷新中' : '⟳' }}
          </button>
        </div>
        <div v-if="unreadCount > 0" class="markall-row">
          <button class="v4-retry markall-btn" :disabled="markingAll" @click="markAllRead">
            {{ markingAll ? '拆信中' : `全部拆完（${unreadCount} 封）` }}
          </button>
        </div>
        <div class="filters">
          <button
            v-for="(label, key) in TYPE_LABELS"
            :key="key"
            class="f"
            :class="{ on: selectedType === key }"
            :aria-pressed="selectedType === key"
            @click="selectedType = key"
          >
            {{ label }}
            <span v-if="key !== 'all' && typeCounts[key]" class="f-count">{{ typeCounts[key] }}</span>
          </button>
        </div>
      </section>

      <!-- 信件列表 -->
      <section class="v4-sec">
        <div v-if="loading" class="skeleton-list">
          <div v-for="i in 4" :key="i" class="v4-skeleton" style="height:74px"></div>
        </div>

        <div v-else-if="loadError && !messages.length" class="v4-err" role="alert">
          信鸽迷路了：{{ loadError }}<br>
          <button class="v4-retry" @click="load(true)">让鸽子再飞一次</button>
        </div>

        <div v-else-if="!filtered.length" class="v4-empty">
          <div class="v4-empty-emoji"><span aria-hidden="true">🕊️</span></div>
          <!-- R53A-4：先判「有筛选条件但无命中」，避免与上方「全部拆完（N 封）」自相矛盾 -->
          <p v-if="keyword || selectedType !== 'all'">这类信鸽还没飞来，换个分类或关键词看看</p>
          <p v-else-if="unreadOnly">未拆的信都拆完啦，鸽子们去歇着了</p>
          <p v-else>栖木上暂时没有鸽子，信鸽明天还会再来</p>
          <button
            v-if="keyword || selectedType !== 'all' || unreadOnly"
            class="v4-retry"
            @click="keyword = ''; selectedType = 'all'; unreadOnly = false"
          >
            清除筛选
          </button>
        </div>

        <template v-else>
          <div
            v-for="msg in filtered"
            :key="msg.id"
            class="pigeon"
            :class="{ unread: !msg.is_read, open: openIds.has(msg.id) }"
          >
            <!-- 鸽子行 -->
            <button
              class="head"
              :aria-expanded="openIds.has(msg.id)"
              :aria-label="`${msg.is_read ? '' : '未读：'}${msg.title}，${fmtRelative(msg.created_at)} · ${TYPE_LABELS[msg.message_type] || '其他来信'}`"
              @click="toggleOpen(msg)"
            >
              <span class="bird" aria-hidden="true">
                <span v-if="!msg.is_read" class="dot"></span>🕊️
              </span>
              <span class="pinfo">
                <span class="ptitle">{{ msg.title }}</span>
                <span class="ptime">{{ fmtRelative(msg.created_at) }} · {{ TYPE_LABELS[msg.message_type] || '📮 其他来信' }}</span>
              </span>
              <span class="parrow" aria-hidden="true">›</span>
            </button>

            <!-- 信箱 -->
            <div v-if="openIds.has(msg.id)" class="letterbox">
              <!-- 未读：火漆封印 -->
              <template v-if="!msg.is_read">
                <button
                  class="wax"
                  :class="{ 'is-unsealing': unsealingIds.has(msg.id) }"
                  :disabled="unsealingIds.has(msg.id)"
                  :aria-label="unsealingIds.has(msg.id) ? '拆信中' : '拆信'"
                  @click.stop="unseal(msg)"
                >
                  {{ unsealingIds.has(msg.id) ? '拆信中' : '封' }}
                </button>
                <div class="seal-hint">火漆封印完好 —— 点一下拆信</div>
              </template>

              <!-- 已读：直接显示正文 -->
              <template v-else>
                <div class="pbody">
                  <div class="pbody-head">
                    <span class="ptag">{{ TYPE_LABELS[msg.message_type] || '📮 其他来信' }}</span>
                    <span class="pbody-time">{{ fmtFull(msg.created_at) }}</span>
                  </div>
                  <div class="pbody-text">{{ msg.content }}</div>
                  <RouterLink
                    v-if="DEEP_LINKS[msg.message_type]"
                    class="deep-link"
                    :to="DEEP_LINKS[msg.message_type].to"
                  >
                    {{ DEEP_LINKS[msg.message_type].label }}
                  </RouterLink>
                </div>
              </template>
            </div>
          </div>
        </template>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { facilityStamp } from '@/config/game'
import { messageApi, type StationMessage } from '@/api'
import { useToast } from '@/composables/useToast'
import { humanizeError } from '@/composables/usePanelApi'

const toast = useToast()

const messages = ref<StationMessage[]>([])
const loading = ref(true)
const loadError = ref('')
const markingAll = ref(false)
/** R36：手动刷新按钮专属忙碌态 —— load(false) 永远不置位 loading，
 *  按钮另绑 refreshing，busy 时禁用防连点；finally 里复位（含错误路径） */
const refreshing = ref(false)

const keyword = ref('')
const unreadOnly = ref(false)
const selectedType = ref<string>('all')
const openIds = ref<Set<number>>(new Set())
const unsealingIds = ref<Set<number>>(new Set())

/** 服务器返回的真实未读数（首屏拉取，后续随本地标记同步） */
const unreadCount = ref(0)

/** 类型值保持原样，只做游戏化标签映射 */
const TYPE_LABELS: Record<string, string> = {
  all: '全部',
  system: '📢 营地广播',
  announcement: '📣 营地公告',
  ticket: '🆘 求救信号',
  subscription: '⚡ 能量到账',
  media_seek: '🏮 许愿进度',
  exchange_code: '🎟️ 兑换码通知',
}

/** 消息类型 → 站内跳转（v4 路径） */
const DEEP_LINKS: Record<string, { to: string; label: string }> = {
  ticket: { to: '/game/signal', label: '去看看求救信号 →' },
  media_seek: { to: '/game/wishlist', label: '去看看许愿池 →' },
  subscription: { to: '/shop', label: '去补给站看看 →' },
  exchange_code: { to: '/shop#shop-redeem', label: '去兑换 →' },
}

const filtered = computed(() => {
  let list = messages.value
  if (selectedType.value !== 'all') list = list.filter((m) => m.message_type === selectedType.value)
  if (unreadOnly.value) list = list.filter((m) => !m.is_read)
  const kw = keyword.value.trim().toLowerCase()
  if (kw) {
    list = list.filter(
      (m) => m.title.toLowerCase().includes(kw) || m.content.toLowerCase().includes(kw),
    )
  }
  return list
})

const typeCounts = computed(() => {
  const map: Record<string, number> = {}
  for (const m of messages.value) map[m.message_type] = (map[m.message_type] || 0) + 1
  return map
})

function toggleOpen(msg: StationMessage) {
  const next = new Set(openIds.value)
  if (next.has(msg.id)) next.delete(msg.id)
  else next.add(msg.id)
  openIds.value = next
}

/** 火漆拆信：点一下 → 标记已读 → 正文展开 */
async function unseal(msg: StationMessage) {
  if (msg.is_read || unsealingIds.value.has(msg.id)) return
  const pending = new Set(unsealingIds.value)
  pending.add(msg.id)
  unsealingIds.value = pending
  try {
    await messageApi.markAsRead(msg.id)
    msg.is_read = true
    unreadCount.value = Math.max(0, unreadCount.value - 1)
    toast.success('信拆开了 ✉️')
  } catch (e) {
    toast.error(humanizeError(e))
  } finally {
    const done = new Set(unsealingIds.value)
    done.delete(msg.id)
    unsealingIds.value = done
  }
}

async function markAllRead() {
  if (markingAll.value) return
  markingAll.value = true
  try {
    await messageApi.markAllRead()
    messages.value.forEach((m) => { m.is_read = true })
    unreadCount.value = 0
    toast.success('信都拆完啦 🕊️')
  } catch (e) {
    toast.error(humanizeError(e))
  } finally {
    markingAll.value = false
  }
}

/** R53A-2：请求序号，只有最新一次 load 的响应才写回状态（防并发互相覆盖） */
let loadRequestId = 0

async function load(showSpinner = true): Promise<boolean> {
  const requestId = ++loadRequestId
  if (showSpinner) loading.value = true
  // R53A-23：手动刷新（showSpinner=false）期间保留错误态，避免错误态先闪成空态，成功后再清
  if (showSpinner) loadError.value = ''
  try {
    const [list, count] = await Promise.all([
      messageApi.getMessages({ unread_only: false, limit: 100 }),
      messageApi.getUnreadCount(),
    ])
    // R53A-2：过期响应直接丢弃，不覆盖更新的数据
    if (requestId !== loadRequestId) return true
    loadError.value = ''
    messages.value = list || []
    unreadCount.value = count?.unread_count ?? messages.value.filter((m) => !m.is_read).length
    // 掉出当前筛选的已展开行，保持展开状态但跟随 is_read 变化
    const alive = new Set(messages.value.map((m) => m.id))
    openIds.value = new Set([...openIds.value].filter((id) => alive.has(id)))
    return true
  } catch (e) {
    // 静默刷新失败保留已有列表；首屏失败才进错误态
    if (requestId !== loadRequestId) return true // 过期请求的失败不再提示，以最新请求为准
    if (showSpinner || !messages.value.length) {
      messages.value = []
      loadError.value = humanizeError(e)
    }
    return false
  } finally {
    // R53A-2：只有最新请求能复位 loading，避免首屏骨架被旧请求提前关掉
    if (requestId === loadRequestId) loading.value = false
  }
}

/** 手动刷新按钮：专属 busy 态，忙时禁用防连点（竞态写回）。
 *  R36：finally 里必复位 —— 成功/失败路径都不会卡死按钮。
 *  R37 P3-4：手动刷新失败给人话 toast，列表保留已有数据不碰。 */
async function onManualRefresh() {
  if (refreshing.value) return
  refreshing.value = true
  try {
    const ok = await load(false)
    if (!ok) toast.error('刷新失败，信鸽可能迷路了')
  } finally {
    refreshing.value = false
  }
}

function fmtFull(iso: string) {
  // R41：非法日期回退原串（照抄 CompanionView R38 P3-10 的 isNaN 回退）
  const d = new Date(iso)
  if (isNaN(d.getTime())) return iso
  return d.toLocaleString('zh-CN', {
    month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit',
  })
}

function fmtRelative(iso: string) {
  // R41：非法日期直接回退原串 —— NaN 差值不再一路漏到 fmtFull
  const d = new Date(iso)
  if (isNaN(d.getTime())) return iso
  const diff = Date.now() - d.getTime()
  const minutes = Math.floor(diff / 60000)
  if (minutes < 1) return '刚刚'
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  return fmtFull(iso)
}

onMounted(() => { void load(true) })
</script>

<style scoped>
/* 栖木场景：设计稿 game-inbox.html 原值 */
.perch { text-align: center; padding: 6px 0 2px; }
.perch .birds { font-size: 44px; letter-spacing: 6px; animation: coo 3s ease-in-out infinite; }
@keyframes coo { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(-6px); } }
.perch .bar {
  width: 240px; height: 10px; background: #8A6F4D; border-radius: 6px;
  margin: 2px auto 0; box-shadow: 0 3px 6px rgba(0, 0, 0, 0.2);
}
.perch-tip { font-size: 12px; color: var(--ink2); margin-top: 10px; }
.perch-tip b { color: var(--red); }

/* 工具条 */
.toolbar { display: flex; gap: 8px; align-items: center; margin-bottom: 12px; }
.search-wrap { position: relative; flex: 1; min-width: 0; }
.search-icon {
  position: absolute; left: 12px; top: 50%; transform: translateY(-50%);
  font-size: 14px; pointer-events: none;
}
/* 减少动态偏好：全局 transform:none 会杀掉 translateY(-50%)，用静态 top 补回垂直居中（输入框 44px / 图标 14px） */
@media (prefers-reduced-motion: reduce) {
  .search-icon { top: 15px; }
}
.search-input {
  width: 100%; height: 44px; padding: 0 12px 0 36px;
  border: 1.5px solid var(--line); border-radius: var(--paper-r-lg, 12px);
  background: var(--paper-card); color: var(--ink); font-size: 16px;
}
.search-input::placeholder { color: var(--ink2); }
.unread-toggle {
  display: inline-flex; align-items: center; gap: 6px;
  height: 44px; padding: 0 14px; border-radius: var(--paper-r-lg, 12px);
  border: 1.5px solid var(--line); background: var(--paper-card);
  color: var(--ink2); font-size: 12px; font-weight: 700; white-space: nowrap; cursor: pointer;
}
.unread-toggle .red-dot {
  width: 9px; height: 9px; border-radius: var(--paper-r-full, 50%); background: var(--red);
  opacity: 0.35;
}
.unread-toggle.on { border-color: var(--red); color: var(--red); }
.unread-toggle.on .red-dot { opacity: 1; animation: blink 1.4s infinite; }
.refresh-btn {
  width: 44px; height: 44px; flex-shrink: 0; border-radius: var(--paper-r-lg, 12px);
  border: 1.5px solid var(--line); background: var(--paper-card);
  color: var(--ink2); font-size: 18px; cursor: pointer;
}
.refresh-btn:disabled { opacity: 0.5; cursor: default; }
/* R28：加载中显示"刷新中"人话文案，按钮自动变宽容纳文字（高度保持 44px） */
.refresh-btn.is-loading { width: auto; padding: 0 14px; font-size: 14px; }
/* R28：拆信中显示"拆信中"人话文案，字号缩小以容纳进火漆圆 */
.wax.is-unsealing { font-size: 13px; }
.markall-row { margin-bottom: 12px; }
.markall-btn {
  width: 100%; min-height: 44px; font-size: 13px;
}

/* 分类标签：设计稿 game-inbox.html 原值 */
.filters { display: flex; gap: 8px; margin-bottom: 14px; flex-wrap: wrap; }
.f {
  font-size: 12px; font-weight: 700; padding: 7px 14px; border-radius: 18px;
  border: 1.5px solid var(--line); background: var(--paper-card); color: var(--ink2); cursor: pointer;
  min-height: 44px;
}
.f.on { background: var(--ink); border-color: var(--ink); color: var(--paper-card); }
.f-count { opacity: 0.85; font-size: 12px; }

/* 鸽子行：设计稿 game-inbox.html 原值 */
.pigeon {
  background: var(--paper-card); border: 1.5px solid var(--line); border-radius: var(--paper-r-lg, 12px);
  margin-bottom: 10px; overflow: hidden;
}
.pigeon.unread { border-color: var(--gold); }
.pigeon .head {
  display: flex; align-items: center; gap: 12px; padding: 13px 14px;
  width: 100%; min-height: 60px; background: none; border: none;
  font: inherit; color: inherit; text-align: left; cursor: pointer;
}
.bird { position: relative; font-size: 30px; line-height: 1; flex-shrink: 0; }
.bird .dot {
  position: absolute; top: -2px; right: -4px; width: 11px; height: 11px;
  border-radius: var(--paper-r-full, 50%); background: var(--red); border: 2px solid var(--paper-card);
  animation: blink 1.4s infinite;
}
@keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
.pinfo { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 4px; }
.ptitle {
  font-size: 14px; font-weight: 700;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.pigeon.unread .ptitle { font-weight: 800; }
.ptime { font-size: 11px; color: var(--ink2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.parrow {
  color: var(--ink2); font-size: 18px; flex-shrink: 0;
  transition: transform 0.2s;
}
.pigeon.open .parrow { transform: rotate(90deg); }

/* 信箱 */
.letterbox {
  border-top: 1px dotted var(--line); padding: 18px; text-align: center;
  animation: fadein 0.35s;
}
@keyframes fadein { from { opacity: 0; transform: translateY(8px); } }

/* 火漆封印：设计稿 game-inbox.html 原值 */
.wax {
  width: 54px; height: 54px; border-radius: var(--paper-r-full, 50%); border: none;
  background: var(--red); color: var(--on-dark, #fff); margin: 0 auto 10px;
  display: flex; align-items: center; justify-content: center;
  font-size: 22px; font-weight: 800; font-family: Georgia, serif;
  box-shadow: 0 3px 0 var(--red-deep, #a33319); cursor: pointer;
  animation: sealpulse 1.8s infinite;
}
.wax:disabled {
  cursor: wait;
  opacity: 0.85;
  background-image: linear-gradient(100deg, rgba(255, 255, 255, 0) 30%, rgba(255, 255, 255, 0.4) 50%, rgba(255, 255, 255, 0) 70%);
  background-size: 220% 100%;
  animation: wax-shimmer 1.2s linear infinite;
}
/* R53A-26：触屏没有 cursor:wait，用高光扫过给出可见的进行中反馈；减少动态偏好下退化为静态终态 */
@keyframes wax-shimmer {
  from { background-position: 140% 0; }
  to { background-position: -60% 0; }
}
@media (prefers-reduced-motion: reduce) {
  .wax:disabled { animation: none; background-image: none; opacity: 0.7; }
}
@keyframes sealpulse { 0%, 100% { transform: scale(1); } 50% { transform: scale(1.07); } }
.seal-hint { font-size: 12px; color: var(--ink2); }

/* 已读信纸 */
.pbody {
  text-align: left; font-size: 13px; line-height: 2;
  background: var(--paper); border: 1px solid var(--line);
  border-radius: var(--paper-r-md, 10px); padding: 14px;
  animation: fadein 0.4s;
}
.pbody-head {
  display: flex; justify-content: space-between; align-items: center;
  border-bottom: 1px dotted var(--line); padding-bottom: 8px; margin-bottom: 8px;
}
.ptag {
  font-size: 11px; font-weight: 700; color: var(--red);
  border: 1px solid var(--red); border-radius: var(--paper-r-md, 10px); padding: 2px 10px;
}
.pbody-time { font-size: 11px; color: var(--ink2); }
.pbody-text { white-space: pre-wrap; word-break: break-word; color: var(--ink); }
.deep-link {
  display: inline-block; margin-top: 12px; min-height: 44px; line-height: 44px;
  padding: 0 18px; border-radius: var(--paper-r-lg, 12px); background: var(--red); color: var(--on-dark, #fff);
  font-size: 13px; font-weight: 700; text-decoration: none;
  box-shadow: 0 3px 0 var(--red-deep, #a33319);
}
.deep-link:active { transform: translateY(2px); box-shadow: none; }

/* 状态区 */
.skeleton-list { display: flex; flex-direction: column; gap: 10px; }
.v4-empty-emoji { font-size: 40px; margin-bottom: 8px; }

/* 键盘可达性：按钮获得焦点时给出可见轮廓 */
.unread-toggle:focus-visible,
.refresh-btn:focus-visible,
.v4-retry:focus-visible,
.markall-btn:focus-visible,
.f:focus-visible,
.head:focus-visible,
.wax:focus-visible {
  outline: 2px solid var(--red);
  outline-offset: 2px;
}

/* R53A-3：.pigeon 的 overflow:hidden 会裁掉外扩轮廓，改用内嵌轮廓让键盘焦点可见 */
.head:focus-visible { outline-offset: -2px; }

/* R27：减少动态偏好下装饰动画停掉（本地守卫）；R28：补 letterbox/pbody 的 fadein（此前靠全局兜底） */
@media (prefers-reduced-motion: reduce) {
  .perch .birds { animation: none; }
  .unread-toggle.on .red-dot,
  .bird .dot { animation: none; opacity: 1; }
  .wax { animation: none; }
  .letterbox, .pbody { animation: none; }
  .deep-link:active { transform: none; }
}
</style>
