<template>
  <div class="v4-scope">
    <div class="v4-page">
      <header class="v4-loghead">
        <span class="v4-stamp">服务器 · 生态</span>
        <h1>我的服务器</h1>
        <p>它活着，而且很忙 —— PR-5 填入生命体 / 雷达 / 生态 / 机关组件</p>
      </header>

      <section class="v4-sec">
        <div class="v4-sec-title">🟢 生命体</div>
        <div class="v4-sec-sub">PR-5 接入呼吸光球 + 拟人化文案</div>
        <PlaceholderBlock :entry="server" label="生命体" />
      </section>

      <section class="v4-sec">
        <div class="v4-sec-title">📡 任务雷达</div>
        <div class="v4-sec-sub">PR-5 接入 canvas 雷达扫描</div>
        <PlaceholderBlock :entry="server" label="雷达" />
      </section>

      <section class="v4-sec">
        <div class="v4-sec-title">🪴 片库生态</div>
        <div class="v4-sec-sub">PR-5 接入植物柱状图</div>
        <PlaceholderBlock :entry="server" label="生态" />
      </section>

      <section class="v4-sec" style="padding-bottom:20px">
        <div class="v4-sec-title">🎛️ 机关</div>
        <div class="v4-sec-sub">PR-5 接入物理拨杆开关（仅管理员可写）</div>
        <PlaceholderBlock :entry="server" label="机关" />
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
const { server } = storeToRefs(panel)

onMounted(() => {
  void panel.loadServer()
})
</script>
