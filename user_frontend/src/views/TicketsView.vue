<script setup lang="ts">
/**
 * 工单页面
 * 用户可以创建工单、查看工单列表、回复工单
 */
import { ref, onMounted, nextTick, computed } from 'vue'
import {
  Ticket,
  Plus,
  MessageSquare,
  Clock,
  CheckCircle,
  XCircle,
  AlertCircle,
  Send,
  X,
  RefreshCw,
  UserCheck,
} from 'lucide-vue-next'
import { ticketApi, type TicketMessage } from '@/api'
import { useToast } from '@/composables/useToast'
import { toastCopy, confirmCopy } from '@/constants/toastMessages'
import { toUserMessage } from '@/utils/toUserMessage'
import EmptyState from '@/components/ui/EmptyState.vue'

const toast = useToast()

interface Ticket {
  id: number
  title: string
  category: string
  priority: string
  status: string
  created_at: string
  updated_at: string
}

// 状态
const tickets = ref<Ticket[]>([])
const loading = ref(false)
/** 列表首屏拉取失败：和「真的没有工单」分开显示 */
const loadError = ref(false)
const refreshing = ref(false)
const selectedTicket = ref<Ticket | null>(null)
const messages = ref<TicketMessage[]>([])
const messagesLoading = ref(false)
const showCreateModal = ref(false)
const showDetailModal = ref(false)
const messagesEl = ref<HTMLElement | null>(null)

// 创建工单表单
const createForm = ref({
  title: '',
  category: 'other',
  message: ''
})
const creating = ref(false)

// 回复工单
const replyMessage = ref('')
const replying = ref(false)

// 状态配置
const statusConfig: Record<string, { label: string; color: string; icon: any }> = {
  open: { label: '待处理', color: 'text-orange-400', icon: Clock },
  pending: { label: '处理中', color: 'text-blue-400', icon: AlertCircle },
  resolved: { label: '已解决', color: 'text-green-400', icon: CheckCircle },
  closed: { label: '已关闭', color: 'text-gray-400', icon: XCircle },
}

const categoryOptions = [
  { value: 'account', label: '账号问题' },
  { value: 'subscription', label: '订阅问题' },
  { value: 'payment', label: '支付问题' },
  { value: 'emby', label: 'Emby问题' },
  { value: 'other', label: '其他问题' },
]

// 获取工单列表
async function fetchTickets(showSpinner = true) {
  if (showSpinner) loading.value = true
  try {
    const res = await ticketApi.getMyTickets()
    tickets.value = (res as any) || []
    loadError.value = false
  } catch (error) {
    console.error('获取工单列表失败:', error)
    if (!tickets.value.length) loadError.value = true
  } finally {
    if (showSpinner) loading.value = false
  }
}

// 打开创建工单弹窗
function openCreateModal() {
  createForm.value = {
    title: '',
    category: 'other',
    message: ''
  }
  showCreateModal.value = true
}

// 标题与描述都填了才能提交（两个字段都是后端必填，先掣一下省一次白跑）
const canCreate = computed(() =>
  !!createForm.value.title.trim() && !!createForm.value.message.trim()
)

// 头部刷新：走与列表加载同一个函数，不弹骨架（只转图标）
async function refreshTicketList() {
  if (refreshing.value) return
  refreshing.value = true
  try {
    await fetchTickets(false)
  } finally {
    refreshing.value = false
  }
}
// 创建工单
async function createTicket() {
  if (!createForm.value.title.trim() || !createForm.value.message.trim()) {
    return
  }

  creating.value = true
  try {
    await ticketApi.create({
      title: createForm.value.title,
      category: createForm.value.category,
      message: createForm.value.message
    })
    toast.success(toastCopy.ticketSubmitted())
    showCreateModal.value = false
    await fetchTickets()
  } catch (error: any) {
    console.error('创建工单失败:', error)
    toast.error(toUserMessage(error, 'ticketCreateFailed'))
  } finally {
    creating.value = false
  }
}

// 打开工单详情
async function openTicketDetail(ticket: Ticket) {
  selectedTicket.value = ticket
  showDetailModal.value = true
  await fetchMessages(ticket.id)
}

// 获取工单消息
async function fetchMessages(ticketId: number, showSpinner = true) {
  if (showSpinner) messagesLoading.value = true
  try {
    const res = await ticketApi.getMessages(ticketId)
    messages.value = (res as any) || []
  } catch (error) {
    console.error('获取消息失败:', error)
  } finally {
    if (showSpinner) messagesLoading.value = false
  }
  await scrollMessagesToBottom()
}

// 新回复进来后滚到底部，用户不用自己找最新的一条
async function scrollMessagesToBottom() {
  await nextTick()
  const el = messagesEl.value
  if (el) el.scrollTop = el.scrollHeight
}

// 回复工单
async function replyTicket() {
  if (!replyMessage.value.trim() || !selectedTicket.value) return

  replying.value = true
  try {
    await ticketApi.reply(selectedTicket.value.id, replyMessage.value)
    replyMessage.value = ''
    await fetchMessages(selectedTicket.value.id, false)
    await fetchTickets(false)
  } catch (error: any) {
    console.error('回复失败:', error)
    toast.error(toUserMessage(error, 'ticketReplyFailed'))
  } finally {
    replying.value = false
  }
}

// 关闭工单
async function closeTicket() {
  if (!selectedTicket.value) return
  if (!confirm(confirmCopy.closeTicket)) return

  try {
    await ticketApi.close(selectedTicket.value.id)
    toast.success(toastCopy.ticketClosed())
    showDetailModal.value = false
    await fetchTickets()
  } catch (error: any) {
    console.error('关闭工单失败:', error)
    toast.error(toUserMessage(error, 'ticketCloseFailed'))
  }
}

// 格式化日期
function formatDate(dateStr: string) {
  const date = new Date(dateStr)
  const now = new Date()
  // 服务器时钟比本机快几秒 / 时区写法不一致时 diff 会是负数：一律当「刚刚」，不出现「-1天前」
  const diff = Math.max(0, now.getTime() - date.getTime())
  const days = Math.floor(diff / (1000 * 60 * 60 * 24))

  if (days === 0) {
    const hours = Math.floor(diff / (1000 * 60 * 60))
    if (hours === 0) {
      const minutes = Math.floor(diff / (1000 * 60))
      return minutes === 0 ? '刚刚' : `${minutes}分钟前`
    }
    return `${hours}小时前`
  } else if (days === 1) {
    return '昨天'
  } else if (days < 7) {
    return `${days}天前`
  }
  return date.toLocaleDateString('zh-CN', {
    month: 'short',
    day: 'numeric'
  })
}

function getCategoryLabel(category: string) {
  const option = categoryOptions.find(c => c.value === category)
  return option ? option.label : '其他问题'
}

onMounted(() => {
  fetchTickets()
})
</script>

<template>
  <div class="au-page is-narrow tickets-page">
    <!-- 页面头部：改用全局页头，与消息 / 求片 / 观看记录同一套排版 -->
    <header class="page-head au-anim-up">
      <div>
        <h1 class="page-title">
          <Ticket :size="20" />
          工单中心
        </h1>
        <p class="page-sub">遇到问题在这里提单，客服回复会推送到消息中心</p>
      </div>
      <div class="head-actions">
        <button class="au-btn au-btn-ghost au-btn-sm" :disabled="refreshing" @click="refreshTicketList">
          <RefreshCw :size="14" :class="{ spinning: refreshing }" />
          刷新
        </button>
        <button @click="openCreateModal" class="btn-create">
          <Plus :size="16" />
          新建工单
        </button>
      </div>
    </header>

    <!-- 工单列表 -->
    <div class="tickets-content">
      <div v-if="loading" class="tickets-list">
        <div v-for="i in 3" :key="i" class="au-skeleton skel" />
      </div>

      <div v-else-if="loadError && tickets.length === 0" class="empty-state" role="alert">
        <div class="empty-icon">
          <Ticket :size="32" />
        </div>
        <p>工单列表暂时读取失败</p>
        <p class="empty-hint">可能是网络波动，稍后再试</p>
        <button @click="fetchTickets()" class="btn btn-secondary">重新加载</button>
      </div>

      <EmptyState
        v-else-if="tickets.length === 0"
        title="还没有工单"
        description="遇到问题可以在这里告诉我们"
      >
        <template #action>
          <button @click="openCreateModal" class="btn btn-primary">
            <Plus :size="16" />
            新建工单
          </button>
        </template>
      </EmptyState>

      <div v-else class="tickets-list">
        <div
          v-for="ticket in tickets"
          :key="ticket.id"
          @click="openTicketDetail(ticket)"
          class="ticket-item glass-card"
        >
          <div class="ticket-header">
            <div class="ticket-info">
              <span class="ticket-title">{{ ticket.title }}</span>
              <span class="ticket-category">{{ getCategoryLabel(ticket.category) }}</span>
            </div>
            <component :is="statusConfig[ticket.status]?.icon || Clock" :size="16" />
          </div>
          <div class="ticket-footer">
            <span class="ticket-status" :class="statusConfig[ticket.status]?.color">
              {{ statusConfig[ticket.status]?.label || '未知' }}
            </span>
            <span class="ticket-time">{{ formatDate(ticket.updated_at) }}</span>
          </div>
        </div>
      </div>
    </div>

    <!-- 创建工单弹窗 -->
    <div v-if="showCreateModal" class="modal-overlay" @click.self="showCreateModal = false">
      <div class="modal glass-card">
        <div class="modal-header">
          <h3>创建工单</h3>
          <button @click="showCreateModal = false" class="btn-close" aria-label="关闭">
            <X :size="18" />
          </button>
        </div>
        <div class="modal-body">
          <div class="form-group">
            <label>问题标题</label>
            <input
              v-model="createForm.title"
              type="text"
              placeholder="简要描述您遇到的问题"
              maxlength="100"
            />
          </div>
          <div class="form-group">
            <label>问题类型</label>
            <select v-model="createForm.category">
              <option
                v-for="option in categoryOptions"
                :key="option.value"
                :value="option.value"
              >
                {{ option.label }}
              </option>
            </select>
          </div>
          <div class="form-group">
            <label>详细描述</label>
            <textarea
              v-model="createForm.message"
              placeholder="请详细描述您的问题，我们会尽快为您处理"
              rows="5"
              maxlength="1000"
            ></textarea>
          </div>
        </div>
        <div class="modal-footer">
          <button @click="showCreateModal = false" class="btn btn-secondary">取消</button>
          <button
            @click="createTicket"
            :disabled="creating || !canCreate"
            class="btn btn-primary"
          >
            {{ creating ? '创建中...' : '提交工单' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 工单详情弹窗 -->
    <div v-if="showDetailModal" class="modal-overlay" @click.self="showDetailModal = false">
      <div class="modal modal-large glass-card">
        <div class="modal-header">
          <h3>{{ selectedTicket?.title }}</h3>
          <button @click="showDetailModal = false" class="btn-close" aria-label="关闭">
            <X :size="18" />
          </button>
        </div>
        <div class="modal-body modal-body-messages">
          <div v-if="messagesLoading" class="messages-list">
            <div v-for="i in 3" :key="i" class="au-skeleton skel-msg" />
          </div>
          <div v-else ref="messagesEl" class="messages-list">
            <p v-if="!messages.length" class="messages-empty">还没有对话内容</p>
            <div
              v-for="msg in messages"
              :key="msg.id"
              class="message-item"
              :class="{ 'message-admin': msg.is_admin }"
            >
              <div class="message-avatar">
                <UserCheck v-if="msg.is_admin" :size="16" />
                <MessageSquare v-else :size="16" />
              </div>
              <div class="message-content">
                <span v-if="msg.is_admin" class="message-author">{{ msg.admin_name || '客服' }}</span>
                <span class="message-text">{{ msg.message }}</span>
                <span class="message-time">{{ formatDate(msg.created_at) }}</span>
              </div>
            </div>
          </div>
        </div>
        <div class="modal-footer">
          <div v-if="selectedTicket?.status !== 'closed'" class="reply-box">
            <input
              v-model="replyMessage"
              type="text"
              maxlength="2000"
              placeholder="输入回复内容..."
              @keyup.enter="replyTicket"
            />
            <button
              @click="replyTicket"
              :disabled="replying || !replyMessage.trim()"
              class="btn-send"
              :title="replying ? '发送中' : '发送回复'"
            >
              <Send :size="16" />
            </button>
          </div>
          <button v-if="selectedTicket?.status !== 'closed'" @click="closeTicket" class="btn btn-secondary">
            关闭工单
          </button>
          <button @click="showDetailModal = false" class="btn btn-secondary">关闭</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 页面骨架与页头都在全局样式里（styles/aurora.css「页面骨架」）；
   这里原先自己写了一套 600px 宽的容器和一份 #fafafa 标题色，
   结果工单页比别的页面窄一截、标题也偏粗白。 */

.btn-create {
  display: inline-flex;
  align-items: center;
  gap: 0.375rem;
  height: 32px;
  padding: 0 0.875rem;
  background: var(--au-primary);
  border: none;
  border-radius: var(--au-r-sm);
  color: var(--au-on-primary);
  font-size: 0.8125rem;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.2s ease;
}

.btn-create:hover {
  background: var(--au-primary-strong);
}

/* 工单列表 */
.tickets-list {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}

.ticket-item {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
  padding: 1rem;
  cursor: pointer;
  transition: all 0.2s ease;
}

.ticket-item:hover {
  background: var(--au-surface-2);
}

.ticket-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.ticket-info {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  flex: 1;
}

.ticket-title {
  font-weight: 500;
  color: var(--au-text);
  font-size: 0.9375rem;
}

.ticket-category {
  padding: 0.125rem 0.5rem;
  background: var(--au-surface-3);
  border-radius: 4px;
  font-size: 0.8125rem;
  color: var(--au-text-2);
}

.ticket-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 0.8125rem;
}

.ticket-status {
  font-weight: 500;
}

/* 状态色收进暗房影院的语义色（状态配置里的 Tailwind 色名只当作钩子用） */
.ticket-status.text-orange-400 { color: var(--au-warning); }
.ticket-status.text-blue-400 { color: var(--au-info); }
.ticket-status.text-green-400 { color: var(--au-success); }
.ticket-status.text-gray-400 { color: var(--au-text-3); }

.ticket-time {
  color: var(--au-text-3);
}

/* 骨架 */
.skel { height: 78px; }
.skel-msg { height: 64px; }

.spinning { animation: au-spin 0.9s linear infinite; }

.messages-empty {
  margin: 0;
  padding: 2rem 0;
  text-align: center;
  font-size: 0.8125rem;
  color: var(--au-text-3);
}

.message-author {
  font-size: 0.8125rem;
  font-weight: 600;
  color: var(--au-primary);
}

/* 加载状态 */
.loading-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 3rem 1rem;
  color: var(--au-text-2);
}

.spinner {
  width: 32px;
  height: 32px;
  border: 2px solid var(--au-border);
  border-top-color: var(--au-primary);
  border-radius: 50%;
  margin-bottom: 0.75rem;
  animation: au-spin 0.8s linear infinite;
}

/* 空状态 */
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 3rem 1rem;
  text-align: center;
}

.empty-icon {
  width: 56px;
  height: 56px;
  border-radius: 14px;
  background: var(--au-surface);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--au-text-3);
  margin-bottom: 1rem;
}

.empty-state p {
  color: var(--au-text-3);
  margin: 0;
}

.empty-hint {
  font-size: 0.8125rem;
  margin: 0.5rem 0 1rem;
}

/* 弹窗 */
.modal-overlay {
  position: fixed;
  inset: 0;
  background: var(--au-scrim);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
  padding: 1rem;
}

.modal {
  width: 100%;
  max-width: 450px;
  max-height: 90vh;
  display: flex;
  flex-direction: column;
  border-radius: 1rem;
  padding: 1.25rem;
  overflow: hidden;
}

.modal-large {
  max-width: 500px;
}

.modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 1rem;
}

.modal-header h3 {
  font-size: 1.125rem;
  font-weight: 600;
  color: var(--au-text);
  margin: 0;
}

.btn-close {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border-radius: 6px;
  border: none;
  background: transparent;
  color: var(--au-text-3);
  cursor: pointer;
  transition: all 0.2s ease;
}

.btn-close:hover {
  background: var(--au-surface-3);
  color: var(--au-text);
}

.modal-body {
  flex: 1;
  overflow-y: auto;
  margin-bottom: 1rem;
}

.modal-body-messages {
  max-height: 400px;
}

.form-group {
  margin-bottom: 1rem;
}

.form-group label {
  display: block;
  font-size: 0.875rem;
  color: var(--au-text-2);
  margin-bottom: 0.5rem;
}

.form-group input,
.form-group select,
.form-group textarea {
  width: 100%;
  padding: 0.75rem;
  background: var(--au-surface);
  border: 1px solid var(--au-border-strong);
  border-radius: 0.5rem;
  color: var(--au-text);
  font-size: 0.875rem;
  transition: all 0.2s ease;
}

.form-group input:focus,
.form-group select:focus,
.form-group textarea:focus {
  outline: none;
  border-color: var(--au-border-focus);
  background: var(--au-surface-2);
}

.form-group textarea {
  resize: vertical;
  min-height: 100px;
}

/* 消息列表 */
.messages-list {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}

.message-item {
  display: flex;
  gap: 0.75rem;
  padding: 0.75rem;
  border-radius: 0.5rem;
  background: var(--au-surface);
}

.message-item.message-admin {
  background: var(--au-primary-soft);
}

.message-avatar {
  width: 32px;
  height: 32px;
  border-radius: 8px;
  background: var(--au-surface-3);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--au-text-2);
  flex-shrink: 0;
}

.message-admin .message-avatar {
  background: var(--au-primary-mid);
  color: var(--au-primary);
}

.message-content {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}

.message-text {
  color: var(--au-text);
  font-size: 0.875rem;
  line-height: 1.5;
  word-break: break-word;
}

.message-time {
  font-size: 0.8125rem;
  color: var(--au-text-3);
}

.modal-footer {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  flex-wrap: wrap;
}

.reply-box {
  flex: 1;
  display: flex;
  gap: 0.5rem;
  min-width: 200px;
}

.reply-box input {
  flex: 1;
  padding: 0.625rem 0.875rem;
  background: var(--au-surface);
  border: 1px solid var(--au-border-strong);
  border-radius: 0.5rem;
  color: var(--au-text);
  font-size: 0.875rem;
}

.reply-box input:focus {
  outline: none;
  border-color: var(--au-border-focus);
}

.btn-send {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  border-radius: 0.5rem;
  border: none;
  background: var(--au-primary);
  color: var(--au-on-primary);
  cursor: pointer;
  transition: all 0.2s ease;
}

.btn-send:hover:not(:disabled) {
  background: var(--au-primary-strong);
}

.btn-send:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* 按钮样式 */
.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 0.5rem;
  padding: 0.625rem 1rem;
  border-radius: 0.5rem;
  font-size: 0.875rem;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.2s ease;
  border: none;
}

.btn-primary {
  background: var(--au-primary);
  color: var(--au-on-primary);
}

.btn-primary:hover {
  background: var(--au-primary-strong);
}

.btn-secondary {
  background: var(--au-surface-2);
  border: 1px solid var(--au-border-strong);
  color: var(--au-text);
}

.btn-secondary:hover {
  background: var(--au-surface-3);
}

/* 响应式 */
@media (max-width: 480px) {
  .head-actions {
    width: 100%;
  }


  .modal-footer {
    flex-direction: column;
  }

  .reply-box {
    width: 100%;
  }

  .btn {
    width: 100%;
  }
}

/* 手机：图标按钮的点按宽度补到 44px（高度由 mobile.css 统一补齐） */
@media (max-width: 768px) {
  .btn-close { min-width: 44px; }
}
</style>