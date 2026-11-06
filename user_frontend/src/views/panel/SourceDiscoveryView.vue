<template>
  <div class="v4-scope">
    <div class="v4-page sd-page">
      <div class="v4-loghead">
        <h1>找源</h1>
        <p>按片名查一查，确认后转存到你自己的盘，存好就能看。</p>
      </div>

      <!-- 找源未开启：总闸关时两个口都 404，这里只说这一句，不假装能用 -->
      <section v-if="discoveryOff" class="v4-sec">
        <div class="v4-empty">找源未开启，稍后再来看看<br><button class="v4-retry" @click="discoveryOff = false">刷新看看</button></div>
      </section>

      <template v-else>
        <section v-if="needLogin" class="v4-sec">
          <div class="v4-empty">找源要先登录<RouterLink to="/login" class="v4-retry">去登录</RouterLink></div>
        </section>

        <!-- 搜索：界面上只填片名；从想看页带进来的编号只在后台用，用户不用填 -->
        <section class="v4-sec">
          <div class="v4-sec-title">查一下能转存的</div>
          <div class="v4-sec-sub">输入片名，看看有没有能转存到你盘里的。</div>
          <div class="sd-searchbar">
            <input
              v-model="titleInput"
              type="text"
              placeholder="输入片名"
              class="sd-input"
              @input="onTitleInput"
              @keydown.enter="search"
            />
            <button class="sd-btn-pri" :disabled="searching || !hasQuery" @click="search">
              {{ searching ? '查着呢…' : '查一下' }}
            </button>
          </div>
          <div v-if="searchErr" class="v4-err" style="margin-top:12px">{{ searchErr }}</div>
          <div v-if="searched && candidates.length === 0 && !searchErr" class="v4-empty">没找到，换个片名试试</div>
          <div v-if="searched && candidates.length > 0" class="v4-sec-sub" style="margin-top:12px">
            共找到 {{ total }} 条，点确认后转存到你的盘，存好就能看。
          </div>
        </section>

        <!-- 结果：只展示 + 确认转存，绝不挂播放入口 -->
        <section v-if="candidates.length > 0" class="v4-sec">
          <div class="v4-sec-title">查到的</div>
          <div class="sd-list">
            <div v-for="c in candidates" :key="candKey(c)" class="sd-card">
              <div class="sd-title">{{ c.local_title }}<span v-if="c.local_year">（{{ c.local_year }}）</span></div>
              <div class="sd-meta">
                <span v-if="c.local_type">类型：{{ c.local_type }}</span>
                <span>来源：{{ c.source_name || c.adapter }}</span>
                <span>置信度：{{ c.confidence }}</span>
                <span v-if="c.resolution">清晰度：{{ c.resolution }}</span>
                <span v-if="c.container">容器：{{ c.container }}</span>
                <span v-if="c.size_bytes">大小：{{ formatSize(c.size_bytes) }}</span>
                <span v-if="c.subtitle_languages">字幕：{{ c.subtitle_languages }}</span>
              </div>
              <div v-if="c.evidence" class="sd-evidence">{{ c.evidence }}</div>
              <div v-if="confirmResult?.key === candKey(c)" class="sd-result">{{ confirmResult.message || confirmStateLabel(confirmResult.state) }}</div>
              <button
                v-if="hasTriple(c)"
                class="v4-retry"
                :disabled="confirming === candKey(c)"
                @click="confirm(c)"
              >{{ confirming === candKey(c) ? '确认中…' : '确认，转存到我的盘' }}</button>
              <div v-else class="v4-empty">这条信息不全，没法转存</div>
            </div>
          </div>
        </section>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import {
  sourceDiscoveryApi,
  type SourceCandidate,
  type SourceConfirmResult,
  type SourceConfirmState,
} from '@/api'

const discoveryOff = ref(false)
const needLogin = ref(false)

const titleInput = ref('')
// 从想看页跳转带进来的编号：只在后台参与查询，界面上不显示、用户不用填
const bgTmdbId = ref('')
const bgImdbId = ref('')
const searching = ref(false)
const searchErr = ref('')
const searched = ref(false)
const candidates = ref<SourceCandidate[]>([])
const total = ref(0)

const confirming = ref('')
const confirmResult = ref<(SourceConfirmResult & { key: string }) | null>(null)

const route = useRoute()

// 从想看页求片行点「找源」进来时带了 title / tmdb_id / imdb_id：
// 片名预填进输入框，编号只留后台，带了任一项就直接搜一次，少一次手输
onMounted(() => {
  const t = route.query.title
  const tm = route.query.tmdb_id
  const im = route.query.imdb_id
  if (typeof t === 'string' && t.trim()) titleInput.value = t.trim()
  if (typeof tm === 'string' && tm.trim()) bgTmdbId.value = tm.trim()
  if (typeof im === 'string' && im.trim()) bgImdbId.value = im.trim()
  if (hasQuery.value) void search()
})

// 用户动手改片名，就是要查别的片：带入的编号随之作废，免得串到上一部片
function onTitleInput() {
  bgTmdbId.value = ''
  bgImdbId.value = ''
}

const hasQuery = computed(
  () => titleInput.value.trim() !== '' || bgTmdbId.value !== '' || bgImdbId.value !== '',
)

function statusOf(err: unknown): number | null {
  const s = (err as { response?: { status?: number } })?.response?.status
  return typeof s === 'number' ? s : null
}

function detailOf(err: unknown): string {
  const d = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  return typeof d === 'string' ? d : ''
}

// 候选键：确认结果只贴到被点的那张卡上；键必须与后端去重键同口径
// （orchestrator 按 (adapter, local_item_id, source_id, upstream_item_id) 去重，
// 同三元组可被多个适配器各出一张卡），只用三元组会在多适配器时串卡。
function candKey(c: SourceCandidate): string {
  return `${c.adapter}-${c.local_item_id}-${c.source_id ?? ''}-${c.upstream_item_id ?? ''}`
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
    queued: '已开始转存，存到你的盘里就能看',
    already_queued: '24 小时内已经在转存了，不用重复点',
    failed: '没转存成，稍后再试',
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
      ...(bgTmdbId.value ? { tmdb_id: bgTmdbId.value } : {}),
      ...(bgImdbId.value ? { imdb_id: bgImdbId.value } : {}),
    })
    candidates.value = res.candidates
    total.value = res.total
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
/* 找源页：与想看页同款纸面口径（输入框/主按钮数值对齐 WantView 的 ww-input/ww-btn-pri） */
.sd-searchbar {
  display: flex;
  gap: 8px;
}
.sd-input {
  flex: 1;
  min-width: 0;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: var(--paper-r-md, 10px);
  background: var(--paper-card);
  color: var(--ink);
  font-size: 14px;
}
.sd-input::placeholder {
  color: var(--line-deep);
}
.sd-btn-pri {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: none;
  cursor: pointer;
  font-weight: 600;
  white-space: nowrap;
  background: #016a71;
  color: #fff;
  border-radius: var(--paper-r-lg, 12px);
  font-size: 14px;
  min-height: 44px;
  padding: 0 20px;
}
.sd-btn-pri:active {
  background: #015a60;
}
.sd-btn-pri:disabled {
  opacity: 0.6;
  cursor: default;
}
.sd-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.sd-card {
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: var(--paper-r-md, 10px);
  background: var(--paper-card);
}
.sd-title {
  font-size: 15px;
  font-weight: 700;
}
.sd-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 16px;
  margin-top: 8px;
  font-size: 13px;
  color: var(--ink2);
}
.sd-evidence {
  margin-top: 8px;
  font-size: 13px;
}
.sd-result {
  margin-top: 10px;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: var(--paper-r-md, 10px);
  background: var(--paper2);
  font-size: 14px;
}

/* 桌面：与手机设施 1:1，只排版不同（页放宽、结果两列） */
@media (min-width: 1024px) {
  .v4-page.sd-page {
    max-width: 960px;
    padding-left: 32px;
    padding-right: 32px;
  }
  .sd-list {
    display: grid;
    grid-template-columns: 1fr 1fr;
    align-items: start;
  }
}
</style>
