<template>
  <div class="v4-scope">
    <div class="v4-page">
      <header class="v4-loghead">
        <span class="v4-stamp">服务器 · 生态</span>
        <h1><span aria-hidden="true">🌱</span> 服务器</h1>
        <p>它不是机器，是你养的一盆生态</p>
      </header>

      <!-- 顶层错误卡：失败时各区收起，只留这一张 -->
      <div v-if="server.error" class="v4-sec">
        <div class="v4-err" role="alert">{{ server.error }}<br><button class="v4-retry" :disabled="server.loading" @click="retryServer">{{ server.loading ? '正在重连…' : '重试' }}</button></div>
      </div>

      <div v-if="!server.error">
      <!-- 生命体 -->
      <section class="v4-sec">
        <div class="v4-sec-title"><span aria-hidden="true">🟢</span> 生命体</div>
        <div class="v4-sec-sub">呼吸的健康球 —— 活着，而且很忙</div>
        <div v-if="server.loading" class="v4-skeleton" style="height: 290px"></div>
        <LifeOrb v-else-if="data?.vitals" :vitals="data.vitals" />
        <div v-else class="v4-empty">生命体还没有苏醒<br><span class="v4-empty-hint">还没连上，点「重新检查」再试一次</span><button class="v4-retry" @click="retryServer">重新检查</button></div>
      </section>

      <!-- 任务雷达 -->
      <section class="v4-sec">
        <div class="v4-sec-title"><span aria-hidden="true">📡</span> 任务雷达</div>
        <div class="v4-sec-sub">服务器里的小家伙们正在哪忙活</div>
        <div v-if="server.loading" class="v4-skeleton" style="height: 275px"></div>
        <RadarCanvas v-else-if="data?.radar_tasks?.length" :tasks="data.radar_tasks" />
        <div v-else class="v4-empty">雷达上空空如也<br><span class="v4-empty-hint">服务器里有任务在跑就会出现在这里，现在没有就是真没任务</span><button class="v4-retry" @click="retryServer">重新检查</button></div>
      </section>

      <!-- 片库生态 -->
      <section class="v4-sec">
        <div class="v4-sec-title"><span aria-hidden="true">🪴</span> 片库生态</div>
        <div class="v4-sec-sub">每个库都是一株植物，长得越高内容越多</div>
        <div v-if="server.loading" class="v4-skeleton" style="height: 190px"></div>
        <EcoGarden v-else-if="data?.eco?.length" :eco="data.eco" />
        <div v-else class="v4-empty">花园里还没有植物<br><span class="v4-empty-hint">媒体库建好后会一株一株长出来</span><button class="v4-retry" @click="retryServer">重新检查</button></div>
      </section>

      <!-- 生长记录 -->
      <section class="v4-sec" style="padding-bottom: calc(20px + env(safe-area-inset-bottom))">
        <div class="v4-sec-title"><span aria-hidden="true">📈</span> 生长记录</div>
        <div class="v4-sec-sub">过去 7 天，片库每天新长了多少</div>
        <div v-if="server.loading" class="v4-skeleton" style="height: 190px"></div>
        <GrowthChart v-else-if="data?.growth?.length" :growth="data.growth" />
        <div v-else class="v4-empty">还没有生长记录<br><span class="v4-empty-hint">片库有新片入库后这里会画出来，空着就是还没长</span><button class="v4-retry" @click="retryServer">重新检查</button></div>
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
.v4-empty-hint {
  display: block;
  margin-top: 6px;
  font-size: 13px;
  color: #6b7280;
}
</style>
