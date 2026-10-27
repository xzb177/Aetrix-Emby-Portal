<template>
  <div class="v4-scope">
    <div class="v4-page">
      <header class="v4-loghead">
        <span class="v4-stamp">服务器状态</span>
        <h1>服务器</h1>
        <p>连接、任务、媒体库和入库趋势</p>
      </header>

      <!-- 顶层错误卡：失败时各区收起，只留这一张 -->
      <div v-if="server.error" class="v4-sec">
        <div class="v4-err" role="alert">{{ server.error }}<br><button class="v4-retry" :disabled="server.loading" @click="retryServer">{{ server.loading ? '正在重连…' : '重试' }}</button></div>
      </div>

      <div v-if="!server.error">
      <!-- 连接状态（可视化仍是 LifeOrb） -->
      <section class="v4-sec">
        <div class="v4-sec-title">服务器状态</div>
        <div class="v4-sec-sub">是否连上，以及当前负载</div>
        <div v-if="server.loading" class="v4-skeleton" style="height: 290px"></div>
        <LifeOrb v-else-if="data?.vitals" :vitals="data.vitals" />
        <div v-else class="v4-empty">暂未连上服务器<br><span class="v4-empty-hint">点「重新检查」再试一次</span><button class="v4-retry" @click="retryServer">重新检查</button></div>
      </section>

      <!-- 任务（可视化仍是 RadarCanvas） -->
      <section class="v4-sec">
        <div class="v4-sec-title">任务</div>
        <div class="v4-sec-sub">当前正在运行的任务</div>
        <div v-if="server.loading" class="v4-skeleton" style="height: 275px"></div>
        <RadarCanvas v-else-if="data?.radar_tasks?.length" :tasks="data.radar_tasks" />
        <div v-else class="v4-empty">当前没有运行中的任务<br><span class="v4-empty-hint">有任务在跑时会显示在这里</span><button class="v4-retry" @click="retryServer">重新检查</button></div>
      </section>

      <!-- 媒体库（可视化仍是 EcoGarden） -->
      <section class="v4-sec">
        <div class="v4-sec-title">媒体库</div>
        <div class="v4-sec-sub">已配置的媒体库</div>
        <div v-if="server.loading" class="v4-skeleton" style="height: 190px"></div>
        <EcoGarden v-else-if="data?.eco?.length" :eco="data.eco" />
        <div v-else class="v4-empty">尚未配置媒体库<br><span class="v4-empty-hint">配置完成后会显示在这里</span><button class="v4-retry" @click="retryServer">重新检查</button></div>
      </section>

      <!-- 入库趋势（可视化仍是 GrowthChart） -->
      <!-- R100-2：safe-area 只由 .v4-page 统一处理（76px + env），这里不再重复加 env，否则刘海机型底部多出一个安全区高度的空白 -->
      <section class="v4-sec" style="padding-bottom: 20px">
        <div class="v4-sec-title">入库趋势</div>
        <div class="v4-sec-sub">过去 7 天新入库的数量</div>
        <div v-if="server.loading" class="v4-skeleton" style="height: 190px"></div>
        <GrowthChart v-else-if="data?.growth?.length" :growth="data.growth" />
        <div v-else class="v4-empty">近 7 天没有入库记录<br><span class="v4-empty-hint">有新片入库后会显示在这里</span><button class="v4-retry" @click="retryServer">重新检查</button></div>
      </section>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { storeToRefs } from 'pinia'
import { usePanelStore } from '@/stores/panel'
import type { ServerData } from '@/composables/usePanelApi'
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
/* 空态第二行指引：小一号、弱化，告诉用户"正常还是故障" */
/* R100-1：硬编码冷灰 #6b7280 改走 --ink2（暖灰 #6B6257），与纸面色系统一 */
.v4-empty-hint {
  display: block;
  margin-top: 6px;
  font-size: 13px;
  color: var(--ink2);
}
</style>
