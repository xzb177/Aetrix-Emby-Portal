<template>
  <div class="follow-wrap">
    <div v-if="followError" class="v4-err">追剧订阅加载失败，请稍后重试<br><button class="v4-retry" @click="loadFollows">重试</button></div>
    <div v-else-if="!follows.length" class="srv-empty">还没有订阅的剧</div>
    <div v-else class="follow-list">
      <div v-for="f in follows" :key="f.follow_id" class="v4-card follow-card">
        <div class="srv-row">
          <div class="srv-info">
            <div class="srv-name">{{ f.series_name || `剧集 #${f.series_item_id}` }}</div>
            <div class="srv-tag">{{ f.status === 'active' ? '订阅中' : '已取消' }}</div>
          </div>
          <button v-if="f.status === 'active'" type="button" class="acct-btn" @click="cancelFollow(f.series_item_id)">取消订阅</button>
        </div>
        <div v-for="ep in f.episodes" :key="ep.episode_id" class="srv-row">
          <div class="srv-info">
            <div class="srv-name">第 {{ ep.season_number ?? '?' }} 季第 {{ ep.episode_number ?? '?' }} 集 · {{ ep.name }}</div>
            <div class="srv-tag">{{ followStateText(ep.state, ep.queue_reason) }}<span v-if="ep.error_detail"> · {{ ep.error_detail }}</span></div>
          </div>
        </div>
      </div>
    </div>
    <div class="v4-card follow-add">
      <div class="acct-field">
        <label class="acct-label" for="follow-series-id">订阅剧集（填剧集条目 id）</label>
        <input id="follow-series-id" v-model="followSeriesId" type="number" placeholder="例如 12345" />
      </div>
      <p v-if="followFormError" class="acct-error" role="alert">{{ followFormError }}</p>
      <button type="button" class="acct-btn primary" :disabled="followSubmitting" @click="handleSubscribeFollow">
        {{ followSubmitting ? '正在订阅…' : '订阅这部剧' }}
      </button>
    </div>
    <p class="follow-note">订阅后新集到货会自动转存到你自己绑定的盘里；无绑定、盘满或转存失败都会明示原因，绝不静默走共享线路。</p>
  </div>
</template>

<script setup lang="ts">
/**
 * 追剧订阅（自旧 AccountSettingsView 原样抽出）：订阅列表（每剧带分集
 * 转存状态）/ 按站内条目 id 订阅 / 取消订阅。挂载即加载，向父级发
 * 'summary'：{ visible, activeCount }——接口 404（开关关）时 visible=
 * false，父级把整行藏掉（与原页口径一致）；其他失败显示错误态重试。
 */
import { onMounted, ref } from 'vue'
import { seriesFollowApi, type SeriesFollow } from '@/api/economy'
import { humanizeError } from '@/composables/usePanelApi'

const emit = defineEmits<{
  (e: 'summary', payload: { visible: boolean; activeCount: number }): void
}>()

const follows = ref<SeriesFollow[]>([])
const followLoaded = ref(false)
const followError = ref('')
const followSeriesId = ref('')
const followFormError = ref('')
const followSubmitting = ref(false)

function emitSummary() {
  emit('summary', {
    visible: followLoaded.value || !!followError.value,
    activeCount: follows.value.filter((f) => f.status === 'active').length,
  })
}
function followStateText(state: string, queueReason: string | null): string {
  if (state === 'ready') return '已转存到你的盘'
  if (state === 'pending') return '转存排队中'
  if (state === 'running') return '转存进行中'
  if (state === 'done') return '转存完成'
  if (state === 'failed') return '转存失败'
  if (queueReason === 'no_binding') return '未绑定网盘，绑定后下一轮自动转存'
  if (queueReason === 'unsupported_provider_115') return '115 已下线，不支持追剧转存'
  if (queueReason === 'unknown_provider') return '来源不明，暂不转存'
  return '等待下一轮扫集'
}
async function loadFollows() {
  followError.value = ''
  try {
    const res = await seriesFollowApi.list()
    follows.value = res.follows || []
    followLoaded.value = true
  } catch (e) {
    // 开关关时后端 404：整块隐藏，不当成加载失败反复提示
    const status = (e as { response?: { status?: number } }).response?.status
    if (status === 404) {
      followLoaded.value = false
      followError.value = ''
    } else {
      followError.value = humanizeError(e)
    }
  }
  emitSummary()
}
async function handleSubscribeFollow() {
  followFormError.value = ''
  const id = Number(followSeriesId.value)
  if (!id) {
    followFormError.value = '请先填剧集条目 id'
    return
  }
  followSubmitting.value = true
  try {
    await seriesFollowApi.subscribe(id)
    followSeriesId.value = ''
    await loadFollows()
  } catch (e) {
    followFormError.value = humanizeError(e)
  } finally {
    followSubmitting.value = false
  }
}
async function cancelFollow(seriesItemId: number) {
  try {
    await seriesFollowApi.unsubscribe(seriesItemId)
    await loadFollows()
  } catch (e) {
    followFormError.value = humanizeError(e)
  }
}

onMounted(() => {
  void loadFollows()
})
</script>

<style scoped>
.follow-wrap { display: flex; flex-direction: column; gap: 10px; }
.follow-list { display: flex; flex-direction: column; gap: 10px; }
.follow-card { padding: 6px 14px; }
.srv-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 10px 0;
}
.srv-row + .srv-row { border-top: 1px dashed var(--paper-line); }
.srv-info { min-width: 0; }
.srv-name {
  font-size: 14px;
  font-weight: 700;
  color: var(--paper-ink);
}
.srv-tag {
  font-size: 11px;
  color: var(--paper-ink-2);
}
.srv-empty {
  padding: 10px 2px;
  font-size: 12px;
  color: var(--paper-ink-2);
}
.follow-add { padding: 14px; }
.acct-field { margin-bottom: 4px; }
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
.acct-error {
  margin: 10px 0 0;
  padding: 8px 12px;
  background: var(--au-danger-soft);
  border-radius: var(--paper-r-sm, 8px);
  color: var(--red);
  font-size: 13px;
}
.acct-btn {
  height: 44px;
  padding: 0 16px;
  margin-top: 10px;
  border-radius: var(--paper-r-md, 10px);
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  background: var(--paper2);
  border: 1px solid var(--line);
  color: var(--ink);
  white-space: nowrap;
}
.follow-card .acct-btn { margin-top: 0; }
.acct-btn.primary {
  background: var(--ink);
  border: 1px solid var(--ink);
  color: var(--on-dark);
  font-weight: 600;
}
.acct-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.follow-note {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--ink2);
}
</style>
