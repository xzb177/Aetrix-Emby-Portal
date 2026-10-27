<template>
  <!-- 合集卡片（发现页合集区段与 /collections 货架页共用）。
       封面只认 cover_url：非空字符串出图，否则单色块 + Library，不拼 URL、不用 emoji。 -->
  <RouterLink
    :to="`/collections/${collection.id}`"
    class="v4-collection-card"
    :aria-label="`合集：${collection.name}`"
  >
    <span class="v4-collection-art" aria-hidden="true">
      <img
        v-if="coverSrc"
        class="v4-collection-cover"
        :src="coverSrc"
        alt=""
        loading="lazy"
        decoding="async"
        @error="coverFailed = true"
      />
      <Library v-else :size="20" :stroke-width="1.75" />
    </span>
    <span class="v4-collection-body">
      <span class="v4-collection-name">{{ collection.name }}</span>
      <span class="v4-collection-meta">{{ collection.member_count }} 部</span>
    </span>
    <span class="v4-collection-go" aria-hidden="true">›</span>
  </RouterLink>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { Library } from 'lucide-vue-next'
import type { ShelfCollection } from '@/api'

const props = defineProps<{ collection: ShelfCollection }>()

const coverFailed = ref(false)

watch(() => props.collection.cover_url, () => {
  coverFailed.value = false
})

/** 非空字符串才当图片地址；不补默认图，也不根据 artwork_mode 推导。 */
const coverSrc = computed(() => {
  if (coverFailed.value) return ''
  const url = props.collection.cover_url
  return typeof url === 'string' && url.trim() !== '' ? url : ''
})
</script>

<style scoped>
.v4-collection-card {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 14px;
  border: 1px solid var(--au-border, #e3e3e2);
  border-radius: 12px;
  background: var(--au-surface, #fff);
  color: inherit;
  text-decoration: none;
}
.v4-collection-art {
  width: 44px;
  height: 58px;
  flex: 0 0 auto;
  border-radius: 6px;
  overflow: hidden;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #8a7a62;
  color: #f6f1e7;
}
.v4-collection-cover {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}
.v4-collection-body {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  flex: 1;
}
.v4-collection-name {
  font-weight: 700;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.v4-collection-meta {
  font-size: 12.5px;
  color: var(--au-text-3, #8a847a);
}
.v4-collection-go {
  font-size: 22px;
  color: var(--au-text-3, #8a847a);
  flex: 0 0 auto;
}
</style>
