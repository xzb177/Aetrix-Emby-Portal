<template>
  <div class="v4-scope">
    <div class="v4-page">
      <header class="v4-loghead">
        <span class="v4-stamp">收藏家 · 手册</span>
        <h1>我的收藏</h1>
        <p>口味、成就、馆灵 —— PR-4 填入护照 / 星图 / 养成组件</p>
      </header>

      <section class="v4-sec">
        <div class="v4-sec-title">🛂 口味护照</div>
        <div class="v4-sec-sub">PR-4 接入 12 格集章网格</div>
        <PlaceholderBlock :entry="collector" label="护照" />
      </section>

      <section class="v4-sec">
        <div class="v4-sec-title">✨ 成就星图</div>
        <div class="v4-sec-sub">PR-4 接入 SVG 星座交互</div>
        <PlaceholderBlock :entry="collector" label="星图" />
      </section>

      <section class="v4-sec" style="padding-bottom:20px">
        <div class="v4-sec-title">🥚 馆灵养成</div>
        <div class="v4-sec-sub">PR-4 接入进化动画 + 进度条</div>
        <PlaceholderBlock :entry="collector" label="馆灵" />
      </section>
    </div>
    <PanelTabbar />
  </div>
</template>

<script setup lang="ts">
import { onMounted } from 'vue'
import { storeToRefs } from 'pinia'
import { usePanelStore } from '@/stores/panel'
import PanelTabbar from '@/components/panel/PanelTabbar.vue'
import PlaceholderBlock from '@/components/panel/PlaceholderBlock.vue'

const panel = usePanelStore()
const { collector } = storeToRefs(panel)

onMounted(() => {
  void panel.loadCollector()
})
</script>
