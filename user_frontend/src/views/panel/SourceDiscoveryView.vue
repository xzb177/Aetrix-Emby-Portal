<template>
  <div class="v4-scope">
    <div class="v4-page">
      <div class="shop-head">
        <span class="shop-stamp">找源</span>
        <h1>找源</h1>
        <p>按标题、TMDB 或 IMDb 编号查来源候选；搜索只读不建任务，确认后才转存到你自己的盘，副本就绪后可播。</p>
      </div>

      <!-- 找源未开启：总闸关时两个口都 404，这里只说这一句，不假装能用 -->
      <section v-if="discoveryOff" class="shop-sec">
        <div class="v4-empty">找源未开启，稍后再来看看<br><button class="v4-retry" @click="discoveryOff = false">刷新看看</button></div>
      </section>

      <template v-else>
        <div v-if="needLogin" class="v4-empty">
          找源要先登录<RouterLink to="/login" class="v4-retry">去登录</RouterLink>
        </div>

        <!-- 搜索：三个条件至少给一个；搜出来只是候选，不建任务 -->
        <section class="shop-sec">
          <div class="v4-sec-title">查来源候选</div>
          <div class="v4-sec-sub">标题、TMDB 编号、IMDb 编号至少填一个</div>
          <div class="src-form">
            <input
              v-model="titleInput"
              type="text"
              placeholder="标题"
              class="src-input"
              @keydown.enter="search"
            />
            <input
              v-model="tmdbInput"
              type="text"
              inputmode="numeric"
              placeholder="TMDB 编号"
              class="src-input"
              @keydown.enter="search"
            />
            <input
              v-model="imdbInput"
              type="text"
              placeholder="IMDb 编号，例如 tt1234567"
              class="src-input"
              @keydown.enter="search"
            />
            <button class="v4-retry" :disabled="searching || !hasQuery" @click="search">
              {{ searching ? '查着呢…' : '查一下' }}
            </button>
          </div>
          <div v-if="searchErr" class="v4-err">{{ searchErr }}</div>
          <div v-if="searched && candidates.length === 0 && !searchErr" class="v4-empty">没查到来源候选，换个条件试试</div>
          <div v-if="searched && candidates.length > 0" class="v4-sec-sub">
            共 {{ total }} 条候选（来源：{{ adapters.join('、') || '—' }}）；候选现在不能直接播，确认后转存到你的盘，副本就绪后可播。
          </div>
        </section>

        <!-- 候选：只展示 + 确认转存，绝不挂播放入口 -->
        <section v-if="candidates.length > 0" class="shop-sec">
          <div class="v4-sec-title">候选</div>
          <div class="src-list">
            <div v-for="c in candidates" :key="`${c.local_item_id}-${c.source_id ?? ''}-${c.upstream_item_id ?? ''}`" class="src-card">
              <div class="src-title">{{ c.local_title }}<span v-if="c.local_year">（{{ c.local_year }}）</span></div>
              <div class="src-meta">
                <span v-if="c.local_type">类型：{{ c.local_type }}</span>
                <span>来源：{{ c.source_name || c.adapter }}</span>
                <span>置信度：{{ c.confidence }}</span>
                <span v-if="c.resolution">清晰度：{{ c.resolution }}</span>
                <span v-if="c.container">容器：{{ c.container }}</span>
                <span v-if="c.size_bytes">大小：{{ formatSize(c.size_bytes) }}</span>
                <span v-if="c.subtitle_languages">字幕：{{ c.subtitle_languages }}</span>
              </div>
              <div v-if="c.evidence" class="src-evidence">{{ c.evidence }}</div>
              <div v-if="confirmResult?.key === candKey(c)" class="src-result">{{ confirmResult.message || confirmStateLabel(confirmResult.state) }}</div>
              <button
                v-if="hasTriple(c)"
                class="v4-retry"
                :disabled="confirming === candKey(c)"
                @click="confirm(c)"
              >{{ confirming === candKey(c) ? '确认中…' : '确认，转存到我的盘' }}</button>
              <div v-else class="v4-empty">这条候选信息不全，确认不了转存</div>
            </div>
          </div>
        </section>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterLink } from 'vue-router'
import {
  sourceDiscoveryApi,
  type SourceCandidate,
  type SourceConfirmResult,
  type SourceConfirmState,
} from '@/api'

const discoveryOff = ref(false)
const needLogin = ref(false)

const titleInput = ref('')
const tmdbInput = ref('')
const imdbInput = ref('')
const searching = ref(false)
const searchErr = ref('')
const searched = ref(false)
const candidates = ref<SourceCandidate[]>([])
const total = ref(0)
const adapters = ref<string[]>([])

const confirming = ref('')
const confirmResult = ref<(SourceConfirmResult & { key: string }) | null>(null)

const hasQuery = computed(
  () => titleInput.value.trim() !== '' || tmdbInput.value.trim() !== '' || imdbInput.value.trim() !== '',
)

function statusOf(err: unknown): number | null {
  const s = (err as { response?: { status?: number } })?.response?.status
  return typeof s === 'number' ? s : null
}

function detailOf(err: unknown): string {
  const d = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  return typeof d === 'string' ? d : ''
}

// 候选键：确认结果只贴到被点的那张卡上
function candKey(c: SourceCandidate): string {
  return `${c.local_item_id}-${c.source_id ?? ''}-${c.upstream_item_id ?? ''}`
}

// 默认模式确认必须给全三元组；缺任何一个都不发请求、不编造
function hasTriple(c: SourceCandidate): boolean {
  return (
    c.source_id !== null &&
    c.source_id !== undefined &&
    (c.upstream_item_id ?? '').trim() !== ''
  )
}

function confirmStateLabel(state: SourceConfirmState): string {
  return {
    queued: '已建用户盘转存任务，副本就绪后可播',
    already_queued: '24h 内已有转存任务，不重复建',
    failed: '转存任务没建成',
  }[state]
}

// 服务端字节数只做单位换算，不改数值口径
function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB`
}

async function search() {
  if (!hasQuery.value) return
  searching.value = true
  searchErr.value = ''
  confirmResult.value = null
  try {
    const res = await sourceDiscoveryApi.search({
      ...(titleInput.value.trim() ? { title: titleInput.value.trim() } : {}),
      ...(tmdbInput.value.trim() ? { tmdb_id: tmdbInput.value.trim() } : {}),
      ...(imdbInput.value.trim() ? { imdb_id: imdbInput.value.trim() } : {}),
    })
    candidates.value = res.candidates
    total.value = res.total
    adapters.value = res.adapters
    searched.value = true
  } catch (err) {
    if (statusOf(err) === 404 && detailOf(err) === '找源未开启') {
      discoveryOff.value = true
    } else if (statusOf(err) === 401) {
      needLogin.value = true
    } else {
      searchErr.value = detailOf(err) || '搜索失败，稍后再试'
    }
    candidates.value = []
  } finally {
    searching.value = false
  }
}

async function confirm(c: SourceCandidate) {
  if (!hasTriple(c)) return
  const key = candKey(c)
  confirming.value = key
  confirmResult.value = null
  try {
    const res = await sourceDiscoveryApi.confirm({
      local_item_id: c.local_item_id,
      source_id: c.source_id as number,
      upstream_item_id: (c.upstream_item_id as string).trim(),
    })
    confirmResult.value = { ...res, key }
  } catch (err) {
    if (statusOf(err) === 404 && detailOf(err) === '找源未开启') {
      discoveryOff.value = true
    } else if (statusOf(err) === 401) {
      needLogin.value = true
    } else {
      // 400/404 的 detail 就是服务端原话（候选不存在、三元组缺失），原样展示
      confirmResult.value = {
        state: 'failed',
        key,
        message: detailOf(err) || '确认失败，稍后再试',
      }
    }
  } finally {
    confirming.value = ''
  }
}
</script>

<style scoped>
.src-form {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 12px;
}
.src-input {
  flex: 1 1 160px;
  min-width: 0;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: transparent;
  color: var(--ink);
  font-size: 14px;
}
.src-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-top: 12px;
}
.src-card {
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: 10px;
}
.src-title {
  font-size: 15px;
  font-weight: 700;
}
.src-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 16px;
  margin-top: 8px;
  font-size: 13px;
  color: var(--ink2);
}
.src-evidence {
  margin-top: 8px;
  font-size: 13px;
}
.src-result {
  margin-top: 10px;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 10px;
  font-size: 14px;
}
</style>
