<script setup lang="ts">
import { ref } from 'vue'
import { CircleStop, Film } from 'lucide-vue-next'
import type { MyPlaybackSession } from '@/api'
import { posterUrl, type EmbyItem } from '@/api/emby'

defineProps<{
  sessions: MyPlaybackSession[]
  stoppingSession: string
}>()

const emit = defineEmits<{
  (e: 'stop-session', session: MyPlaybackSession): void
}>()

// 正在播放的缩略图：会话里只有 item_id，按 Emby 图片端点拼海报地址；加载失败就退回图标。
// 纯视图状态（img @error 回退），不调接口，随组件走。
const failedThumbs = ref<string[]>([])
function sessionThumb(s: MyPlaybackSession): string {
  if (!s.item_id || failedThumbs.value.includes(s.item_id)) return ''
  return posterUrl({ Id: s.item_id, ImageTags: { Primary: '1' } } as unknown as EmbyItem, 160)
}
function onThumbError(s: MyPlaybackSession) {
  failedThumbs.value = [...failedThumbs.value, s.item_id]
}
</script>

<template>
  <!-- 正在播放：有会话才出现（一条状态，不是管理清单）；完整清单在个人中心 -->
  <section v-if="sessions.length" class="au-anim-up">
    <div class="program-head">
      <p class="program-eyebrow">放映中</p>
      <div class="program-row">
        <h2 class="program-title"><span class="live-dot" aria-hidden="true"></span>正在放映</h2>
        <span class="program-more">{{ sessions.length }} 台设备</span>
      </div>
    </div>
    <div class="playing-list">
      <div v-for="s in sessions" :key="s.session_key" class="playing-row">
        <RouterLink :to="`/media/${s.item_id}`" class="playing-thumb" :aria-label="s.item">
          <img
            v-if="sessionThumb(s)"
            :src="sessionThumb(s)"
            alt=""
            loading="lazy"
            decoding="async"
            @error="onThumbError(s)"
          />
          <Film v-else :size="18" aria-hidden="true" />
        </RouterLink>
        <div class="playing-main">
          <div class="playing-title">
            <RouterLink :to="`/media/${s.item_id}`">{{ s.item }}</RouterLink>
            <span v-if="s.is_paused" class="playing-tag">已暂停</span>
          </div>
          <div class="playing-meta">
            <span>{{ s.device || '未知设备' }}</span>
            <template v-if="s.client">
              <span class="dot">·</span>
              <span>{{ s.client }}</span>
            </template>
            <template v-if="s.play_method">
              <span class="dot">·</span>
              <span>{{ s.play_method === 'Transcode' ? '转码' : '直连' }}</span>
            </template>
          </div>
          <div class="playing-bar">
            <div class="playing-fill" :style="{ width: s.progress + '%' }"></div>
          </div>
        </div>
        <button
          class="au-btn au-btn-danger au-btn-sm"
          :disabled="stoppingSession === s.session_key"
          @click="emit('stop-session', s)"
        >
          <CircleStop :size="13" />
          {{ stoppingSession === s.session_key ? '结束中' : '结束' }}
        </button>
      </div>
    </div>
  </section>
</template>

<style scoped>
/* 从 HomeView 整体迁移（template + script + style）。
   区块头排版（.program-*）已上浮为 L2 全局类（aurora.css），本组件直接复用。 */
.playing-list {
  display: flex;
  flex-direction: column;
  background: var(--au-surface);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-lg);
}

.playing-row {
  display: flex;
  align-items: center;
  gap: 0.875rem;
  padding: 0.875rem 1rem;
}

.playing-row + .playing-row { border-top: 1px solid var(--au-border); }

.playing-thumb {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 44px;
  aspect-ratio: 2 / 3;
  overflow: hidden;
  border-radius: 6px;
  background: var(--au-surface-2);
  color: var(--au-text-4);
}

.playing-thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.playing-main {
  flex: 1;
  min-width: 0;
}

.playing-title {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  min-width: 0;
  font-size: 0.9375rem;
  font-weight: 600;
}

.playing-title a {
  min-height: 0;
  color: var(--au-text);
  text-decoration: none;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.playing-title a:hover { color: var(--au-primary); }

.playing-tag {
  flex-shrink: 0;
  font-size: 0.8125rem;
  font-weight: 500;
  color: var(--au-text-3);
}

.playing-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 0.25rem;
  margin-top: 0.25rem;
  font-size: 0.8125rem;
  color: var(--au-text-3);
}

.playing-meta .dot { color: var(--au-text-4); }

.playing-bar {
  height: 2px;
  margin-top: 0.625rem;
  background: var(--au-track);
  border-radius: 1px;
  overflow: hidden;
}

.playing-fill {
  height: 100%;
  background: var(--au-primary);
}

/* 放映中：标题前一颗呼吸圆点（全页唯一一处动态强调色，"决定性瞬间"才用琥珀） */
.live-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  margin-right: 0.625rem;
  border-radius: 50%;
  background: var(--au-primary);
  vertical-align: 0.125em;
  animation: au-pulse-soft 1.6s ease-in-out infinite;
}

@media (prefers-reduced-motion: reduce) {
  .live-dot { animation: none; }
}
</style>
