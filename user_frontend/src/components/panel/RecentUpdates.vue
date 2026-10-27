<!-- 桌面发现页中栏 · 最近更新（媒体库学习 L6 首页分区之一）
     数据源：首页分区口 continue_watching 之外的 recently_updated 分区
     （启用库的 movie / series，按入库时间倒序；身份门开时只出已绑定条目）。
     本批只做展示（标题 + 年份 + 类型）；行不是链接：本站没有单片详情页，
     不在这里编一个假跳转。点播请到播放器里搜片名（见播放器选择页说明）。 -->
<template>
  <section id="disc-recent" class="d3-card" aria-label="最近更新">
    <div class="d3-sec-head">
      <div>
        <span class="d3-k">RECENT</span>
        <div class="d3-sec-title">最近更新</div>
        <div class="d3-sec-sub">刚入库的片子</div>
      </div>
    </div>
    <div v-if="items.length === 0" class="d3-empty">最近没有新入库的内容</div>
    <div v-else>
      <div v-for="it in items" :key="it.id" class="d3-recent-row">
        <span class="d3-recent-t">{{ it.name || '未命名' }}</span>
        <span class="d3-recent-m">
          <span v-if="it.production_year">{{ it.production_year }}</span>
          <span>{{ it.item_type === 'series' ? '剧集' : '电影' }}</span>
        </span>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import type { RecentAddedItem } from '@/api'

defineProps<{ items: RecentAddedItem[] }>()
</script>

<style scoped>
.d3-recent-row {
  display: flex; justify-content: space-between; align-items: baseline;
  gap: 12px; padding: 8px 0; border-bottom: 1px solid rgba(0, 0, 0, 0.08);
}
.d3-recent-row:last-child { border-bottom: none; }
.d3-recent-t { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.d3-recent-m { display: flex; gap: 8px; flex-shrink: 0; font-size: 13px; opacity: 0.7; }
</style>
