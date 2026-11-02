<template>
  <div class="v4-scope">
    <div class="v4-page">
      <!-- 顶栏 -->
      <div class="v4-col-top">
        <h1>我的收藏</h1>
      </div>

      <!-- 顶层错误卡 -->
      <div v-if="error" class="v4-sec">
        <div class="v4-err" role="alert">收藏没加载出来：{{ error }}<br><button class="v4-retry" :disabled="loading" @click="load">{{ loading ? '正在重试…' : '重试' }}</button></div>
      </div>

      <section v-else class="v4-sec">
        <div class="v4-sec-title">收藏的片子</div>
        <div class="v4-sec-sub">在第三方播放器里标过星的片子都在这里；点卡片右上角的星可以取消收藏。播放还是去播放器里打开，本页不放播放入口。</div>
        <div v-if="loading" class="v4-skeleton" aria-hidden="true" style="height:160px"></div>
        <div v-else-if="items.length === 0" class="v4-empty">还没有收藏 —— 在播放器里给喜欢的片子标个星，就会到这里来</div>
        <div v-else class="fav-grid">
          <article v-for="m in items" :key="m.id" class="fav-card">
            <div class="fav-poster">
              <img
                v-if="posterSrc(m)"
                class="fav-img"
                :src="posterSrc(m)"
                alt=""
                loading="lazy"
                decoding="async"
                @error="onPosterError(m.id)"
              />
              <Clapperboard v-else :size="26" :stroke-width="1.5" class="fav-fallback" aria-hidden="true" />
              <button
                type="button"
                class="fav-star"
                :disabled="pendingId === m.id"
                :aria-label="`取消收藏《${m.name}》`"
                title="取消收藏"
                @click="toggle(m)"
              >
                <Star :size="17" :stroke-width="2" fill="currentColor" />
              </button>
            </div>
            <div class="fav-body">
              <div class="fav-name" :title="m.name">{{ m.name }}</div>
              <div class="fav-meta">{{ metaLine(m) }}</div>
            </div>
          </article>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { Clapperboard, Star } from 'lucide-vue-next'
import { embyApi, type PortalMediaItem } from '@/api'
import { imageAuthUrl } from '@/api/emby'
import { embyApiKeyRef } from '@/api/imageKey'
import { useToast } from '@/composables/useToast'

const toast = useToast()

const loading = ref(false)
const error = ref('')
const items = ref<PortalMediaItem[]>([])
const pendingId = ref<string | null>(null)
const brokenPosters = ref<Set<string>>(new Set())

function errText(err: unknown): string {
  const msg = (err as { userMessage?: string })?.userMessage
  if (msg) return msg
  if (err instanceof Error) return err.message
  return '加载失败'
}

function posterSrc(m: PortalMediaItem): string {
  if (brokenPosters.value.has(m.id)) return ''
  const url = m.poster_url
  // PA-036：后端给的 poster_url 不带凭据，渲染前补 api_key（图片端点已鉴权）
  return typeof url === 'string' && url.trim() !== '' ? imageAuthUrl(url) : ''
}

function onPosterError(id: string) {
  brokenPosters.value = new Set(brokenPosters.value).add(id)
}

// PA-054：图片 key 一变（别页轮换后经 storage 同步过来、或本页读卡拿到
// 新 key），裂图标记立刻清掉——posterSrc 重新拼上新 key 的地址自动重试，
// 不用整页重载才恢复。key 本身没变时不触发，不会把正常裂图反复重刷。
watch(embyApiKeyRef, () => {
  if (brokenPosters.value.size > 0) brokenPosters.value = new Set()
})

function metaLine(m: PortalMediaItem): string {
  const parts: string[] = []
  parts.push(m.type === 'movie' ? '电影' : m.type === 'series' ? '剧集' : m.type === 'season' ? '季' : m.type === 'episode' ? '集' : m.type)
  if (m.year) parts.push(String(m.year))
  if (m.rating) parts.push(`★ ${m.rating.toFixed(1)}`)
  return parts.join(' · ')
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await embyApi.getFavorites()
    items.value = data.items || []
  } catch (err) {
    error.value = errText(err)
  } finally {
    loading.value = false
  }
}

async function toggle(m: PortalMediaItem) {
  if (pendingId.value) return
  pendingId.value = m.id
  try {
    const res = await embyApi.toggleFavorite(m.id)
    if (res.is_favorite) {
      // 理论上不会发生（列表里都是已收藏），但后端说了算：状态对齐即可
      toast.info(`《${m.name}》已在收藏里`)
    } else {
      items.value = items.value.filter((x) => x.id !== m.id)
      toast.success(`已取消收藏《${m.name}》`)
    }
  } catch (err) {
    // 失败明示、不动列表：卡片还留在原地，用户可以再点
    toast.error(`取消收藏失败：${errText(err)}`)
  } finally {
    pendingId.value = null
  }
}

onMounted(load)
</script>

<style scoped>
.fav-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(104px, 1fr));
  gap: 14px 10px;
  margin-top: 12px;
}
.fav-card {
  min-width: 0;
}
.fav-poster {
  position: relative;
  aspect-ratio: 2 / 3;
  border: 1px solid var(--paper2, #e3e3e2);
  border-radius: 10px;
  background: var(--au-bg-soft, #f4f3f0);
  overflow: hidden;
  display: flex;
  align-items: center;
  justify-content: center;
}
.fav-img {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.fav-fallback {
  color: var(--au-text-3, #a8a29a);
}
.fav-star {
  position: absolute;
  top: 6px;
  right: 6px;
  width: 30px;
  height: 30px;
  border: 1px solid var(--paper2, #e3e3e2);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.92);
  color: #016a71;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  padding: 0;
}
.fav-star:disabled {
  opacity: 0.55;
  cursor: wait;
}
.fav-body {
  padding-top: 6px;
}
.fav-name {
  font-size: 13px;
  line-height: 1.35;
  color: var(--ink, #27251e);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.fav-meta {
  margin-top: 2px;
  font-size: 11px;
  color: var(--ink2, #6f6a5e);
}
</style>
