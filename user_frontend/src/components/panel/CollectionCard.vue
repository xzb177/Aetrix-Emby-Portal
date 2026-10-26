<template>
  <!-- 合集卡片（发现页合集区段与 /collections 货架页共用；封面只出占位，不另起封面机制） -->
  <RouterLink
    :to="`/collections/${collection.id}`"
    class="v4-collection-card"
    :aria-label="`合集：${collection.name}`"
  >
    <span class="v4-collection-ic" aria-hidden="true">📚</span>
    <span class="v4-collection-body">
      <span class="v4-collection-name">{{ collection.name }}</span>
      <span class="v4-collection-meta">{{ collection.member_count }} 部</span>
    </span>
    <span class="v4-collection-go" aria-hidden="true">›</span>
  </RouterLink>
</template>

<script setup lang="ts">
import { RouterLink } from 'vue-router'
import type { ShelfCollection } from '@/api'

defineProps<{ collection: ShelfCollection }>()
</script>

<style scoped>
.v4-collection-card {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 14px;
  border: 1px solid var(--au-line, #e3e3e2);
  border-radius: 12px;
  background: var(--au-card, #fff);
  color: inherit;
  text-decoration: none;
}
.v4-collection-ic {
  font-size: 22px;
  flex: 0 0 auto;
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
