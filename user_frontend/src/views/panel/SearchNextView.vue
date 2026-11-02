<template>
  <div class="search-next">
    <h1>搜索</h1>
    <p class="hint">搜本地片单数据，不调外部服务。库内有片的会标出来。</p>

    <form class="search-bar" @submit.prevent="onSearch">
      <input v-model="q" type="search" placeholder="输入片名" aria-label="搜索片名" />
      <select v-model="type" aria-label="类型">
        <option value="all">全部</option>
        <option value="movie">电影</option>
        <option value="tv">剧集</option>
      </select>
      <button type="submit" :disabled="loading || !q.trim()">搜</button>
    </form>

    <p v-if="error" class="error">{{ error }}</p>
    <p v-else-if="freshness" class="freshness">
      数据集共 {{ freshness.total_rows }} 条
      <span v-if="freshness.synced">，上次同步 {{ freshness.last_success_at }}</span>
      <span v-else>，还没同步过</span>
      <span v-if="freshness.last_error">（同步有问题：{{ freshness.last_error }}）</span>
    </p>

    <p v-if="searched && !loading && results.length === 0" class="empty">没搜到，换个词试试。</p>

    <ul v-if="results.length" class="results">
      <li v-for="r in results" :key="next302ResultKey(r)">
        <button class="result" type="button" @click="openDetails(r)">
          <span class="title">{{ r.title }}<span v-if="r.year">（{{ r.year }}）</span></span>
          <span class="meta">{{ r.media_type === 'tv' ? '剧集' : '电影' }} · {{ r.genres.join(' / ') }}</span>
          <span v-if="!r.library_known" class="badge unknown">库内关系未知</span>
          <span v-else-if="r.in_library" class="badge in-library">库内有片</span>
          <span v-else class="badge not-in-library">库里还没有</span>
        </button>
      </li>
    </ul>

    <section v-if="detail" class="detail">
      <h2>{{ detail.title }}<span v-if="detail.year">（{{ detail.year }}）</span></h2>
      <p v-if="detail.overview" class="overview">{{ detail.overview }}</p>
      <p v-if="!detail.library_known" class="note">库内关系没算出来，暂时不标库内/库外。</p>
      <template v-else-if="detail.in_library && detail.library_item_id">
        <p v-if="playStatusLoading">正在查播放状态…</p>
        <p v-else-if="playStatusError" class="error">{{ playStatusError }}</p>
        <div v-else-if="playStatus" class="play-status">
          <p>{{ playStatus.detail }}</p>
          <!-- 动作只给文字指引：建转存任务是商店那单的想看按钮，这里不造假按钮 -->
          <p v-if="playStatus.action === 'play'" class="note">有你自己盘的副本，走既有播放链打开。</p>
          <p v-else-if="playStatus.action === 'wait'" class="note">转存完成后就能播，先等等。</p>
          <p v-else-if="playStatus.action === 'failed'" class="note">转存失败了，不播共享原件，请重试或联系管理员。</p>
          <p v-else-if="playStatus.action === 'want'" class="note">还没有自己的副本，去想看建一个转存任务，转完就能播。</p>
          <p v-else-if="playStatus.action === 'bind'" class="note">去绑定自己的盘后才能点播。</p>
        </div>
        <SubtitlePicker :key="detail.library_item_id" :item-id="detail.library_item_id" />
      </template>
      <p v-else class="note">库里还没有这部片子，这里只展示，不给播放入口。想看入口随商店单来。</p>
      <button type="button" @click="detail = null">关闭详情</button>
    </section>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import {
  next302Api,
  next302ResultKey,
  type Next302Freshness,
  type Next302PlayStatus,
  type Next302Result,
} from '@/api'
import SubtitlePicker from '@/components/panel/SubtitlePicker.vue'

const q = ref('')
const type = ref('all')
const loading = ref(false)
const searched = ref(false)
const error = ref('')
const results = ref<Next302Result[]>([])
const freshness = ref<Next302Freshness | null>(null)
const detail = ref<Next302Result | null>(null)
const playStatus = ref<Next302PlayStatus | null>(null)
const playStatusLoading = ref(false)
const playStatusError = ref('')

function errText(e: unknown): string {
  const status = (e as { response?: { status?: number } })?.response?.status
  if (status === 404) return '302 Next 还没开启（或这部片子不在数据集里）。'
  const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
  return typeof msg === 'string' && msg ? msg : '请求失败，稍后再试。'
}

async function onSearch() {
  if (!q.value.trim()) return
  loading.value = true
  error.value = ''
  detail.value = null
  try {
    const res = await next302Api.search(q.value.trim(), type.value)
    results.value = res.results
    freshness.value = res.freshness
    searched.value = true
  } catch (e: unknown) {
    error.value = errText(e)
    results.value = []
  } finally {
    loading.value = false
  }
}

async function openDetails(r: Next302Result) {
  detail.value = null
  playStatus.value = null
  playStatusError.value = ''
  try {
    // 库内补排行不在数据集里（可能没有 tmdb_id）：按本地条目 id 取详情，
    // 拿空 tmdb_id 查数据集详情必然 404。
    detail.value = !r.dataset_only && r.library_item_id
      ? await next302Api.detailsByItem(r.library_item_id)
      : await next302Api.details(r.media_type, r.tmdb_id)
  } catch (e: unknown) {
    error.value = errText(e)
    return
  }
  if (detail.value.in_library && detail.value.library_item_id) {
    playStatusLoading.value = true
    try {
      playStatus.value = await next302Api.playStatus(detail.value.library_item_id)
    } catch (e: unknown) {
      playStatusError.value = errText(e)
    } finally {
      playStatusLoading.value = false
    }
  }
}
</script>

<style scoped>
.search-next {
  max-width: 720px;
  margin: 0 auto;
  padding: 16px;
}

.search-bar {
  display: flex;
  gap: 8px;
  margin: 12px 0;
}

.search-bar input {
  flex: 1;
  min-width: 0;
}

.hint,
.freshness,
.note {
  color: #666;
  font-size: 14px;
}

.error {
  color: #b3261e;
}

.results {
  list-style: none;
  padding: 0;
  margin: 0;
}

.result {
  display: block;
  width: 100%;
  text-align: left;
  padding: 10px 0;
  border: none;
  border-bottom: 1px solid #eee;
  background: none;
  cursor: pointer;
}

.title {
  display: block;
  font-size: 16px;
}

.meta {
  color: #666;
  font-size: 13px;
}

.badge {
  display: inline-block;
  margin-top: 4px;
  padding: 1px 6px;
  border-radius: 10px;
  font-size: 12px;
  background: #eee;
}

.badge.in-library {
  background: #d7ecec;
}

.detail {
  margin-top: 24px;
  padding-top: 16px;
  border-top: 1px solid #ddd;
}

.overview {
  white-space: pre-wrap;
}
</style>
