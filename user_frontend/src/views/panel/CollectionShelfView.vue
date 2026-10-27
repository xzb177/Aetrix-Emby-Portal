<template>
  <div class="v4-scope">
    <div class="v4-page">
      <!-- 顶栏 -->
      <div class="v4-col-top">
        <h1>合集</h1>
      </div>

      <!-- 顶层错误卡 -->
      <div v-if="error" class="v4-sec">
        <div class="v4-err" role="alert">合集没加载出来：{{ error }}<br><button class="v4-retry" :disabled="loading" @click="load(true)">{{ loading ? '正在重试…' : '重试' }}</button></div>
      </div>

      <!-- 总闸关时：不是报错，是功能未开 -->
      <div v-else-if="gateOff" class="v4-sec">
        <div class="v4-empty">合集功能暂未开启</div>
      </div>

      <section v-else class="v4-sec">
        <div class="v4-sec-title">合集货架</div>
        <div class="v4-sec-sub">服主整理好的合集，按架序排列 —— 点进去看成员</div>
        <div v-if="loading" class="v4-skeleton" aria-hidden="true" style="height:120px"></div>
        <div v-else-if="collections.length === 0" class="v4-empty">还没有合集</div>
        <div v-else class="v4-collections">
          <CollectionCard
            v-for="c in collections"
            :key="c.id"
            :collection="c"
          />
        </div>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import CollectionCard from '@/components/panel/CollectionCard.vue'
import { collectionsApi, type ShelfCollection } from '@/api'

const loading = ref(false)
const error = ref('')
const gateOff = ref(false)
const collections = ref<ShelfCollection[]>([])

function errText(err: unknown): string {
  const msg = (err as { userMessage?: string })?.userMessage
  if (msg) return msg
  if (err instanceof Error) return err.message
  return '加载失败'
}

async function load(force = false) {
  if (loading.value && !force) return
  loading.value = true
  error.value = ''
  gateOff.value = false
  try {
    const data = await collectionsApi.getShelves()
    // 后端已按 sort_order、id 出架，前端不再二次排序
    collections.value = Array.isArray(data?.collections) ? data.collections : []
  } catch (err: unknown) {
    const status = (err as { response?: { status?: number } })?.response?.status
    if (status === 404) {
      // 总闸 COLLECTIONS_ENABLED 关时读口 404：按“功能未开”处理，不报错
      gateOff.value = true
    } else {
      error.value = errText(err)
    }
  } finally {
    loading.value = false
  }
}

onMounted(() => { void load() })
</script>

<style scoped>
.v4-collections {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-top: 12px;
}
</style>
