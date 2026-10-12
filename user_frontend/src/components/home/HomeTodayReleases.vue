<script setup lang="ts">
import { ChevronRight } from 'lucide-vue-next'
import MediaCard from '@/components/media/MediaCard.vue'
import MediaRow from '@/components/media/MediaRow.vue'

export interface RecentCard {
  key: string
  title: string
  year: string
  /** 当前要试的图片地址；空串 = 候选已试完，走排版占位卡 */
  poster: string
  href: string
}

defineProps<{
  items: RecentCard[]
  hasToday: boolean
}>()

defineEmits<{
  (e: 'poster-error', card: RecentCard): void
}>()
</script>

<template>
  <!-- 今日上映：近 7 天入库的竖版海报横滑（追新日历同一份数据），
       点击与追新日历一致走 Rex deep link；没有数据整段不渲染 -->
  <section v-if="items.length" class="au-anim-up">
    <div class="program-head">
      <p class="program-eyebrow">本周排片</p>
      <div class="program-row">
        <h2 class="program-title">{{ hasToday ? '今日上映' : '本周上映' }}</h2>
        <RouterLink to="/calendar" class="program-more">
          追新日历
          <ChevronRight :size="14" />
        </RouterLink>
      </div>
    </div>
    <MediaRow>
      <MediaCard
        v-for="card in items"
        :key="card.key"
        :title="card.title"
        :year="card.year"
        :poster="card.poster"
        :href="card.href"
        @poster-error="$emit('poster-error', card)"
      />
    </MediaRow>
  </section>
</template>

<style scoped>
/* 从 HomeView 整体迁移（template + script + style）。
   区块头排版（.program-*）已上浮为 L2 全局类（aurora.css），本组件直接复用；
   本区块无私有样式、无私有 @media。 */
</style>
