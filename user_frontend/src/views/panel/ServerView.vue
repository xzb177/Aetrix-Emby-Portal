<template>
  <div class="v4-scope">
    <div class="v4-page">
      <header class="v4-loghead">
        <span class="v4-stamp">服务器 · 生态</span>
        <h1>🌱 服务器</h1>
        <p>它不是机器，是你养的一盆生态</p>
      </header>

      <!-- 顶层错误卡：失败时各区收起，只留这一张 -->
      <div v-if="server.error" class="v4-sec">
        <div class="v4-err">服务器没连上：{{ server.error }}<br><button class="v4-retry" @click="retryServer">重试</button></div>
      </div>

      <div v-if="!server.error">
      <!-- 生命体 -->
      <section class="v4-sec">
        <div class="v4-sec-title">🟢 生命体</div>
        <div class="v4-sec-sub">呼吸的健康球 —— 活着，而且很忙</div>
        <div v-if="server.loading" class="v4-skeleton" style="height: 260px"></div>
        <LifeOrb v-else-if="data?.vitals" :vitals="data.vitals" />
        <div v-else class="v4-empty">生命体还没有苏醒</div>
      </section>

      <!-- 任务雷达 -->
      <section class="v4-sec">
        <div class="v4-sec-title">📡 任务雷达</div>
        <div class="v4-sec-sub">后台的小家伙们正在哪忙活 —— 此刻快照</div>
        <div v-if="server.loading" class="v4-skeleton" style="height: 280px"></div>
        <RadarCanvas v-else-if="data?.radar_tasks?.length" :tasks="data.radar_tasks" />
        <div v-else class="v4-empty">雷达上空空如也</div>
      </section>

      <!-- 片库生态 -->
      <section class="v4-sec">
        <div class="v4-sec-title">🪴 片库生态</div>
        <div class="v4-sec-sub">每个库都是一株植物，长得越高内容越多</div>
        <div v-if="server.loading" class="v4-skeleton" style="height: 190px"></div>
        <EcoGarden v-else-if="data?.eco?.length" :eco="data.eco" />
        <div v-else class="v4-empty">花园里还没有植物</div>
      </section>

      <!-- 生长记录 -->
      <section class="v4-sec" style="padding-bottom: 20px">
        <div class="v4-sec-title">📈 生长记录</div>
        <div class="v4-sec-sub">过去 7 天，片库每天新长了多少</div>
        <div v-if="server.loading" class="v4-skeleton" style="height: 190px"></div>
        <GrowthChart v-else-if="data?.growth?.length" :growth="data.growth" />
        <div v-else class="v4-empty">还没有生长记录</div>
      </section>
      </div>
    </div>
    <PanelTabbar />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { storeToRefs } from 'pinia'
import { usePanelStore } from '@/stores/panel'
import type { ServerData } from '@/composables/usePanelApi'
import PanelTabbar from '@/components/panel/PanelTabbar.vue'
import LifeOrb from '@/components/panel/LifeOrb.vue'
import RadarCanvas from '@/components/panel/RadarCanvas.vue'
import EcoGarden from '@/components/panel/EcoGarden.vue'
import GrowthChart from '@/components/panel/GrowthChart.vue'

const panel = usePanelStore()
const { server } = storeToRefs(panel)

const data = computed(() => (server.value.data || null) as ServerData | null)

onMounted(() => {
  void panel.loadServer()
})

function retryServer() {
  void panel.loadServer(true)
}
</script>

<style scoped>
/* 生长记录区无额外样式，复用 v4-sec */
</style>
