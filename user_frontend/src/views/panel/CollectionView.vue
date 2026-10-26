<template>
  <div class="v4-scope">
    <div class="v4-page">
      <div class="v4-col-top">
        <h1>
          <RouterLink to="/collections" class="v4-back" aria-label="返回合集货架">‹</RouterLink>
          <span aria-hidden="true">📚</span> {{ detail?.name ?? '合集' }}
        </h1>
      </div>

      <!-- 顶层错误卡 -->
      <div v-if="error" class="v4-sec">
        <div class="v4-err" role="alert">{{ error }}<br><button class="v4-retry" :disabled="loading" @click="reload">{{ loading ? '正在重试…' : '重试' }}</button></div>
      </div>

      <!-- 合集不存在 / hidden / 总闸关：后端一律 404，前端不区分原因 -->
      <div v-else-if="notFound" class="v4-sec">
        <div class="v4-empty">这个合集不存在或暂未开放<br><RouterLink class="v4-empty-link" to="/collections">回合集货架</RouterLink></div>
      </div>

      <template v-else>
        <!-- 双轨切换：上映序 / 观影序（后端 order_members 的 track 语义） -->
        <section class="v4-sec">
          <div class="v4-sec-title">成员</div>
          <div class="v4-sec-sub">
            <span v-if="detail">{{ detail.effective_count }} 部有效成员</span>
            <span v-else>正在加载…</span>
          </div>
          <div class="v4-track-switch" role="tablist" aria-label="成员排序">
            <button
              type="button"
              role="tab"
              :aria-selected="track === 'release'"
              :class="{ on: track === 'release' }"
              @click="switchTrack('release')"
            >上映序</button>
            <button
              type="button"
              role="tab"
              :aria-selected="track === 'watch'"
              :class="{ on: track === 'watch' }"
              @click="switchTrack('watch')"
            >观影序</button>
          </div>

          <div v-if="loading && items.length === 0" class="v4-skeleton" aria-hidden="true" style="height:120px"></div>
          <!-- 未达 min 门：后端返回空 items + shelf_eligible=false，不谎称有内容 -->
          <div v-else-if="!shelfEligible && items.length === 0" class="v4-empty">
            这个合集还没达到上架门槛
            <span v-if="detail">（{{ detail.effective_count }} / {{ detail.min_effective_members }} 部有效成员）</span>
          </div>
          <div v-else-if="items.length === 0" class="v4-empty">这个合集还没有成员</div>
          <ol v-else class="v4-members">
            <!-- 序号 = 本轨列表中的位置（后端 order_index 是成员存储序，只用于观影轨排序键，不直接展示） -->
            <li v-for="(m, i) in items" :key="m.id" class="v4-member">
              <span class="v4-member-idx">{{ i + 1 }}</span>
              <span class="v4-member-body">
                <span class="v4-member-name">{{ m.name }}</span>
                <span class="v4-member-meta">{{ memberMeta(m) }}</span>
              </span>
            </li>
          </ol>
          <div v-if="hasMore && !loading" class="v4-more">
            <button type="button" class="v4-retry" @click="loadMore">加载更多</button>
          </div>
          <div v-if="loading && items.length > 0" class="v4-skeleton" aria-hidden="true" style="height:48px;margin-top:10px"></div>
        </section>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { collectionsApi, type CollectionDetail, type CollectionMemberItem } from '@/api'

const route = useRoute()
const setId = Number(route.params.id)

const loading = ref(false)
const error = ref('')
const notFound = ref(false)
const detail = ref<CollectionDetail | null>(null)
const track = ref<'release' | 'watch'>('release')
const items = ref<CollectionMemberItem[]>([])
const total = ref(0)
const shelfEligible = ref(true)
const PAGE = 50

function errText(err: unknown): string {
  const msg = (err as { userMessage?: string })?.userMessage
  if (msg) return msg
  if (err instanceof Error) return err.message
  return '加载失败'
}

function memberMeta(m: CollectionMemberItem): string {
  const parts: string[] = []
  if (m.item_type) parts.push(m.item_type === 'movie' ? '电影' : m.item_type === 'series' ? '剧集' : m.item_type)
  if (m.production_year) parts.push(String(m.production_year))
  return parts.join(' · ')
}

async function loadItems(reset: boolean) {
  const offset = reset ? 0 : items.value.length
  const data = await collectionsApi.getItems(setId, { track: track.value, limit: PAGE, offset })
  shelfEligible.value = data.shelf_eligible
  total.value = data.total
  items.value = reset ? data.items : [...items.value, ...data.items]
}

async function reload() {
  if (Number.isNaN(setId)) {
    notFound.value = true
    return
  }
  loading.value = true
  error.value = ''
  notFound.value = false
  try {
    detail.value = await collectionsApi.getCollection(setId)
    await loadItems(true)
  } catch (err: unknown) {
    const status = (err as { response?: { status?: number } })?.response?.status
    if (status === 404) {
      notFound.value = true
    } else {
      error.value = errText(err)
    }
  } finally {
    loading.value = false
  }
}

function switchTrack(t: 'release' | 'watch') {
  if (track.value === t) return
  track.value = t
  void reload()
}

function loadMore() {
  if (loading.value) return
  loading.value = true
  loadItems(false)
    .catch((err: unknown) => { error.value = errText(err) })
    .finally(() => { loading.value = false })
}

const hasMore = computed(() => items.value.length < total.value)

onMounted(() => { void reload() })
</script>

<style scoped>
.v4-back {
  text-decoration: none;
  color: inherit;
  font-size: 28px;
  margin-right: 4px;
}
.v4-track-switch {
  display: flex;
  gap: 8px;
  margin: 12px 0 4px;
}
.v4-track-switch button {
  padding: 6px 16px;
  border-radius: 999px;
  border: 1px solid var(--au-border, #e3e3e2);
  background: transparent;
  color: inherit;
  cursor: pointer;
}
.v4-track-switch button.on {
  background: var(--au-primary, #016a71);
  border-color: var(--au-primary, #016a71);
  color: #fff;
}
.v4-members {
  list-style: none;
  margin: 12px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.v4-member {
  display: flex;
  align-items: baseline;
  gap: 10px;
  padding: 10px 12px;
  border: 1px solid var(--au-border, #e3e3e2);
  border-radius: 10px;
}
.v4-member-idx {
  flex: 0 0 auto;
  min-width: 22px;
  text-align: right;
  font-size: 12.5px;
  color: var(--au-text-3, #8a847a);
}
.v4-member-body {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.v4-member-name {
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.v4-member-meta {
  font-size: 12.5px;
  color: var(--au-text-3, #8a847a);
}
.v4-more {
  margin-top: 12px;
  text-align: center;
}
</style>
