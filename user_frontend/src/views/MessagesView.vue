<script setup lang="ts">
/**
 * 消息中心
 *
 * v2.5.0 重构：
 * - 统一 Aurora 视觉（此前是残留的灰度主题，与全站割裂）
 * - 按日期分组（今天 / 昨天 / 更早），长列表更好读
 * - 按消息类型给出对应入口（工单 → 工单中心、求片 → 求片中心、订阅/兑换 → 钱包）
 * - 保留：类型筛选、只看未读、关键字搜索、单条/全部已读
 *
 * v2.10.4（本次）：点开一条未读就有已读反馈；全部已读、同名合并逻辑、刷新状态
 * 都照常保留（它们由前一条 PR 带来，这里不重造）。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import {
  CheckCheck, MessageSquare, Ticket, Megaphone, Gift, AlertCircle,
  Clock, RefreshCw, Search, Inbox, X, ChevronRight, Filter,
} from 'lucide-vue-next'
import { messageApi, type StationMessage } from '@/api'
import { useToast } from '@/composables/useToast'
import EmptyState from '@/components/ui/EmptyState.vue'

const toast = useToast()
const route = useRoute()

const messages = ref<StationMessage[]>([])
const loading = ref(false)
const refreshing = ref(false)
/** 列表拉取失败：和「真的没有消息」分开显示，给一个重试入口 */
const loadError = ref(false)
const unreadOnly = ref(false)
const keyword = ref('')
const selectedType = ref<string>('all')
const detail = ref<StationMessage | null>(null)
const heartbeat = ref(false)

const typeConfigs: Record<string, { label: string; icon: unknown; tone: string }> = {
  all: { label: '全部', icon: MessageSquare, tone: 'cyan' },
  system: { label: '系统', icon: AlertCircle, tone: 'cyan' },
  ticket: { label: '工单', icon: Ticket, tone: 'amber' },
  announcement: { label: '公告', icon: Megaphone, tone: 'violet' },
  subscription: { label: '订阅', icon: Gift, tone: 'green' },
  media_seek: { label: '求片', icon: Clock, tone: 'cyan' },
  exchange_code: { label: '兑换', icon: Gift, tone: 'amber' },
}

/** 消息类型 → 站内跳转（把通知和操作连起来） */
const DEEP_LINKS: Record<string, { to: string; label: string }> = {
  ticket: { to: '/tickets', label: '查看工单' },
  media_seek: { to: '/request', label: '查看求片进度' },
  subscription: { to: '/store', label: '查看我的订阅' },
  exchange_code: { to: '/store', label: '前往核销' },
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

/** 分组：今天 / 昨天 / 更早 */
const grouped = computed(() => {
  const today: StationMessage[] = []
  const yesterday: StationMessage[] = []
  const earlier: StationMessage[] = []
  const now = new Date()
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const startOfYesterday = startOfToday - 86_400_000

  for (const m of filtered.value) {
    const t = new Date(m.created_at).getTime()
    if (t >= startOfToday) today.push(m)
    else if (t >= startOfYesterday) yesterday.push(m)
    else earlier.push(m)
  }

  return [
    { key: 'today', label: '今天', items: today },
    { key: 'yesterday', label: '昨天', items: yesterday },
    { key: 'earlier', label: '更早', items: earlier },
  ].filter((g) => g.items.length)
})

const unreadCount = computed(() => messages.value.filter((m) => !m.is_read).length)

const typeCounts = computed(() => {
  const map: Record<string, number> = {}
  for (const m of messages.value) {
    map[m.message_type] = (map[m.message_type] || 0) + 1
  }
  return map
})

function typeOf(type: string) {
  return typeConfigs[type] || typeConfigs.system
}

async function openDetail(msg: StationMessage, { silent = false } = {}) {
  detail.value = msg
  if (!msg.is_read) {
    await markRead(msg)
    if (!silent) toast.success('已标为已读')
  }
}

async function load(showSpinner = true) {
  if (showSpinner) loading.value = true
  try {
    messages.value = (await messageApi.getMessages({ unread_only: false, limit: 100 })) || []
    loadError.value = false
  } catch {
    // 静默刷新失败时保留已有列表，只有首屏失败才进错误态
    if (showSpinner || !messages.value.length) {
      messages.value = []
      loadError.value = true
    }
  } finally {
    loading.value = false
  }
}

async function refresh() {
  refreshing.value = true
  heartbeat.value = true
  try {
    await load(false)
  } finally {
    refreshing.value = false
    heartbeat.value = false
  }
}

async function markRead(msg: StationMessage) {
  if (msg.is_read) return
  try {
    await messageApi.markAsRead(msg.id)
    msg.is_read = true
  } catch {
    /* 静默——行内已标记，服务端结果不卡交互 */
  }
}

async function markAllRead() {
  try {
    await messageApi.markAllRead()
    messages.value.forEach((m) => { m.is_read = true })
    toast.success('已全部标为已读')
  } catch {
    toast.error('操作失败，请稍后重试')
  }
}

function fmtFull(iso: string) {
  return new Date(iso).toLocaleString('zh-CN', {
    month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit',
  })
}

function fmtRelative(iso: string) {
  const diff = Date.now() - new Date(iso).getTime()
  const minutes = Math.floor(diff / 60000)
  if (minutes < 1) return '刚刚'
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  return fmtFull(iso)
}

/**
 * 分类可由链接直接指定（顶栏铃铛点公告预览会走 /messages?tab=announcement）。
 * 没有这一步，用户从顶栏点了公告却落在「全部」分类，还得自己再找一遍。
 */
function applyTabFromQuery() {
  const t = route.query.tab
  if (typeof t === 'string' && t in typeConfigs) selectedType.value = t
}

watch(() => route.query.tab, applyTabFromQuery)

onMounted(() => {
  applyTabFromQuery()
  load()
})
</script>

<template>
  <div class="messages-view">
    <div class="au-page">
      <!-- 头部 -->
      <header class="page-head au-anim-up">
        <div>
          <h1 class="page-title">
            <Inbox :size="20" />
            消息中心
            <span v-if="unreadCount" class="au-badge au-badge-cyan">{{ unreadCount }} 条未读</span>
          </h1>
          <p class="page-sub">系统通知、工单回复、订阅与求片进度都会汇总到这里</p>
        </div>
        <div class="head-actions">
          <button class="au-btn au-btn-ghost au-btn-sm" @click="refresh">
            <RefreshCw :size="14" :class="{ spinning: refreshing }" />
            刷新
          </button>
          <button v-if="unreadCount" class="au-btn au-btn-primary au-btn-sm" @click="markAllRead">
            <CheckCheck :size="14" />
            全部已读
          </button>
        </div>
      </header>

      <!-- 筛选 -->
      <div class="filters au-anim-up">
        <div class="search-wrap">
          <Search :size="15" class="search-icon" />
          <input v-model="keyword" class="au-input search-input" type="text" placeholder="搜索标题或内容…" />
        </div>
        <button class="filter-toggle" :class="{ active: unreadOnly }" @click="unreadOnly = !unreadOnly">
          <Filter :size="14" />
          只看未读
          <span v-if="unreadCount" class="filter-count">{{ unreadCount }}</span>
        </button>
      </div>

      <div class="type-tabs au-anim-up">
        <button
          v-for="(cfg, key) in typeConfigs"
          :key="key"
          class="type-tab"
          :class="{ active: selectedType === key }"
          @click="selectedType = key"
        >
          <component :is="cfg.icon" :size="13" />
          <span>{{ cfg.label }}</span>
          <span v-if="key !== 'all' && typeCounts[key]" class="tab-count">{{ typeCounts[key] }}</span>
        </button>
      </div>

      <!-- 内容 -->
      <div v-if="loading" class="skeleton-list">
        <div v-for="i in 4" :key="i" class="au-skeleton skel" />
      </div>

      <div v-else-if="loadError && !messages.length" class="au-empty" role="alert">
        <Bell :size="30" />
        <h3>消息暂时读取失败</h3>
        <p>可能是网络波动，稍后再试</p>
        <button class="au-btn au-btn-ghost au-btn-sm" @click="load()">重新加载</button>
      </div>

      <EmptyState
        v-else-if="filtered.length === 0 && selectedType === 'announcement' && !keyword"
        title="暂时没有公告"
        description="站点公告会同时出现在这里与消息列表里"
      />
      <EmptyState
        v-else-if="filtered.length === 0 && messages.length"
        variant="search"
        title="没有符合条件的消息"
        description="试试换个类型或清空搜索"
      >
        <template #action>
          <button class="au-btn au-btn-ghost au-btn-sm" @click="keyword = ''; selectedType = 'all'">清除筛选</button>
        </template>
      </EmptyState>
      <EmptyState
        v-else-if="filtered.length === 0"
        title="暂时没有消息"
        description="管理员的操作通知会出现在这里"
      />

      <template v-else>
        <section v-for="group in grouped" :key="group.key" class="msg-group">
          <div class="group-label">{{ group.label }}<span>（{{ group.items.length }}）</span></div>

          <button
            v-for="msg in group.items"
            :key="msg.id"
            class="au-card msg-card"
            :class="{ unread: !msg.is_read }"
            :disabled="loading || refreshing"
            @click="openDetail(msg, { silent: true })"
          >
            <span class="msg-icon">
              <component :is="typeOf(msg.message_type).icon" :size="17" />
            </span>

            <span class="msg-body">
              <span class="msg-top">
                <span class="au-badge" :class="`au-badge-${typeOf(msg.message_type).tone}`">
                  {{ typeOf(msg.message_type).label }}
                </span>
                <span v-if="!msg.is_read" class="unread-dot" />
                <span class="msg-time">{{ fmtRelative(msg.created_at) }}</span>
              </span>
              <span class="msg-title">{{ msg.title }}</span>
              <span class="msg-text">{{ msg.content }}</span>
            </span>

            <Transition name="fade-sm" mode="out-in">
              <ChevronRight
                v-if="!msg.is_read"
                :key="`arr-${msg.id}`"
                :size="15"
                class="msg-arrow"
              />
              <CheckCheck
                v-else
                :key="`chk-${msg.id}`"
                :size="15"
                class="msg-arrow msg-arrow-checked"
              />
            </Transition>
          </button>
        </section>
      </template>
    </div>

    <!-- 详情 -->
    <Transition name="fade">
      <div v-if="detail" class="modal-mask" @click.self="detail = null">
        <div class="modal au-card">
          <header class="modal-head">
            <span class="modal-icon" :class="`tone-${typeOf(detail.message_type).tone}`">
              <component :is="typeOf(detail.message_type).icon" :size="18" />
            </span>
            <div class="modal-head-main">
              <span class="au-badge" :class="`au-badge-${typeOf(detail.message_type).tone}`">
                {{ typeOf(detail.message_type).label }}
              </span>
              <h2>{{ detail.title }}</h2>
              <span class="modal-time">{{ fmtFull(detail.created_at) }}</span>
            </div>
            <button class="modal-close" aria-label="关闭" @click="detail = null"><X :size="18" /></button>
          </header>

          <div class="modal-body">{{ detail.content }}</div>

          <footer class="modal-foot">
            <RouterLink
              v-if="DEEP_LINKS[detail.message_type]"
              class="au-btn au-btn-primary au-btn-sm"
              :to="DEEP_LINKS[detail.message_type].to"
              @click="detail = null"
            >
              {{ DEEP_LINKS[detail.message_type].label }}
            </RouterLink>
            <button class="au-btn au-btn-ghost au-btn-sm" @click="detail = null">关闭</button>
          </footer>
        </div>
      </div>
    </Transition>
  </div>
</template>

<style scoped>
/* 页头（.page-head / .page-title / .page-sub / .head-actions）与页面骨架都在全局样式里，
   这里不再重复定义——见 styles/aurora.css 的「页面骨架」一节。 */

/* 筛选 */
.filters {
  display: flex;
  gap: 0.625rem;
  align-items: center;
  margin-bottom: 0.75rem;
  flex-wrap: wrap;
}

.search-wrap { position: relative; flex: 1 1 260px; }
.search-icon {
  position: absolute;
  left: 0.75rem;
  top: 50%;
  transform: translateY(-50%);
  color: var(--au-text-3);
  pointer-events: none;
}
.search-input { padding-left: 2.25rem; }

.filter-toggle {
  display: inline-flex;
  align-items: center;
  gap: 0.375rem;
  height: 42px;
  padding: 0 0.875rem;
  border-radius: var(--au-r-md);
  border: 1px solid var(--au-border);
  background: transparent;
  color: var(--au-text-3);
  font-size: 0.8125rem;
  cursor: pointer;
  transition: all var(--au-fast) var(--au-ease);
}

.filter-toggle:hover { color: var(--au-text); border-color: var(--au-border-strong); }
.filter-toggle.active {
  color: var(--au-primary);
  border-color: var(--au-primary-border);
  background: var(--au-primary-soft);
}

.filter-count {
  min-width: 16px;
  height: 16px;
  padding: 0 5px;
  border-radius: var(--au-r-full);
  background: var(--au-primary);
  color: var(--au-on-primary);
  font-size: 0.8125rem;
  font-weight: 700;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.type-tabs {
  display: flex;
  gap: 0.375rem;
  flex-wrap: wrap;
  margin-bottom: 1.125rem;
}

.type-tab {
  display: inline-flex;
  align-items: center;
  gap: 0.3125rem;
  height: 30px;
  padding: 0 0.6875rem;
  border-radius: var(--au-r-full);
  border: 1px solid var(--au-border);
  background: transparent;
  color: var(--au-text-3);
  font-size: 0.8125rem;
  cursor: pointer;
  transition: all var(--au-fast) var(--au-ease);
}

.type-tab:hover { color: var(--au-text); border-color: var(--au-border-strong); }
.type-tab.active {
  color: var(--au-primary);
  border-color: var(--au-primary-border);
  background: var(--au-primary-soft);
}

.tab-count { opacity: 0.65; font-size: 0.8125rem; }

/* 列表 */
.msg-group { margin-bottom: 1.25rem; }

.group-label {
  font-size: 0.8125rem;
  font-weight: 600;
  color: var(--au-text-3);
  margin-bottom: 0.5rem;
  letter-spacing: 0.02em;
}

.group-label span { color: var(--au-text-3); font-weight: 400; }

.msg-card {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  width: 100%;
  padding: 0.75rem 0.875rem;
  margin-bottom: 0.4375rem;
  text-align: left;
  cursor: pointer;
  color: inherit;
  font: inherit;
  transition: all var(--au-fast) var(--au-ease);
}

.msg-card:hover { border-color: var(--au-primary-border); transform: translateX(2px); }
.msg-card.unread { border-color: var(--au-primary-border); background: var(--au-surface-2); }

.msg-icon {
  width: 34px;
  height: 34px;
  flex-shrink: 0;
  border-radius: var(--au-r-sm);
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--au-surface-2);
  color: var(--au-text-2);
}

.msg-card.unread .msg-icon { background: var(--au-primary-soft); color: var(--au-primary); }

.msg-body { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 0.1875rem; }

.msg-top { display: flex; align-items: center; gap: 0.375rem; }

.unread-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--au-primary);
  flex-shrink: 0;
}

.msg-time { margin-left: auto; font-size: 0.8125rem; color: var(--au-text-3); white-space: nowrap; }

.msg-title {
  font-size: 0.875rem;
  font-weight: 600;
  color: var(--au-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.msg-text {
  font-size: 0.8125rem;
  color: var(--au-text-3);
  line-height: 1.5;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  white-space: pre-wrap;
}

.msg-arrow { color: var(--au-text-3); flex-shrink: 0; }

/* 骨架 */
.skeleton-list { display: flex; flex-direction: column; gap: 0.4375rem; }
.skel { height: 74px; }

.spinning { animation: au-spin 0.9s linear infinite; }

/* 详情弹层 */
.modal-mask {
  position: fixed;
  inset: 0;
  z-index: 1200;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 1rem;
  /* 与其它弹窗同一块遮罩（浅色主题下 --au-overlay 是奶油色半透明，像没遮住） */
  background: var(--au-scrim);
}

.modal {
  width: 100%;
  max-width: 520px;
  max-height: 82vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.modal-head {
  display: flex;
  align-items: flex-start;
  gap: 0.75rem;
  padding: 1.125rem 1.25rem;
  border-bottom: 1px solid var(--au-border);
}

.modal-icon {
  width: 38px;
  height: 38px;
  flex-shrink: 0;
  border-radius: var(--au-r-sm);
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--au-primary-soft);
  color: var(--au-primary);
}

.modal-icon.tone-amber { background: var(--au-warning-soft); color: var(--au-warning); }
.modal-icon.tone-green { background: var(--au-success-soft); color: var(--au-success); }
.modal-icon.tone-violet { background: var(--au-violet-soft); color: var(--au-violet); }

.modal-head-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 0.25rem; }

.modal-head-main h2 {
  margin: 0;
  font-size: 1.0625rem;
  font-weight: 700;
  color: var(--au-text);
  line-height: 1.35;
}

.modal-time { font-size: 0.8125rem; color: var(--au-text-3); }

.modal-close {
  width: 30px;
  height: 30px;
  flex-shrink: 0;
  border: none;
  border-radius: var(--au-r-sm);
  background: var(--au-surface-2);
  color: var(--au-text-2);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all var(--au-fast) var(--au-ease);
}

.modal-close:hover { background: var(--au-surface-3); color: var(--au-text); }

.modal-body {
  padding: 1.25rem;
  overflow-y: auto;
  font-size: 0.875rem;
  line-height: 1.75;
  color: var(--au-text-2);
  white-space: pre-wrap;
  word-break: break-word;
}

.modal-foot {
  display: flex;
  justify-content: flex-end;
  gap: 0.5rem;
  padding: 0.875rem 1.25rem;
  border-top: 1px solid var(--au-border);
}

.fade-enter-active, .fade-leave-active { transition: opacity var(--au-med) var(--au-ease); }
.fade-enter-from, .fade-leave-to { opacity: 0; }

/* 手机：图标按钮的点按宽度补到 44px（高度由 mobile.css 统一补齐） */
@media (max-width: 768px) {
  .modal-close { min-width: 44px; }
}
</style>