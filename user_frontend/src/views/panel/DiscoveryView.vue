<template>
  <div class="v4-scope">
    <div class="v4-page">
      <!-- 日志头：问候 + 封存件数 + 连签（PR-3 填 ParcelBox/EcgCanvas/WaxLetter） -->
      <header class="v4-loghead">
        <span class="v4-stamp">探险日志 · {{ dateLabel }}</span>
        <h1>{{ greeting }}，{{ username }}</h1>
        <p v-if="today.data">今天片库里有 {{ parcelCount }} 件新封存，等你拆。</p>
        <div v-else-if="today.loading" class="v4-skeleton" style="height:28px;width:70%;margin-top:10px"></div>
        <div v-else-if="today.error" class="v4-err" style="margin-top:10px">今日概览加载失败：{{ today.error }}</div>
        <div v-if="streak != null" class="v4-fire">🔥 连签 <b>{{ streak }}</b> 天 · 明天有惊喜</div>
      </header>

      <section class="v4-sec">
        <div class="v4-sec-title">📦 今日封存</div>
        <div class="v4-sec-sub">新入库的片子，先拆为敬 —— PR-3 接入开封动画</div>
        <div v-if="parcels.loading" class="v4-skeleton" style="height:120px"></div>
        <div v-else-if="parcels.error" class="v4-err">封存加载失败：{{ parcels.error }}</div>
        <pre v-else class="v4-err" style="white-space:pre-wrap">{{ parcelText }}</pre>
      </section>

      <section class="v4-sec">
        <div class="v4-sec-title">💓 服务器脉搏</div>
        <div class="v4-sec-sub">PR-3 接入心电图画布 + 活动文案轮播</div>
        <div v-if="pulse.loading" class="v4-skeleton" style="height:60px"></div>
        <div v-else-if="pulse.error" class="v4-err">脉搏加载失败：{{ pulse.error }}</div>
        <pre v-else class="v4-err" style="white-space:pre-wrap">{{ pulseText }}</pre>
      </section>

      <section class="v4-sec" style="padding-bottom:20px">
        <div class="v4-sec-title">✉️ 每日一封</div>
        <div class="v4-sec-sub">PR-3 接入火漆拆信动画</div>
        <div v-if="letter.loading" class="v4-skeleton" style="height:60px"></div>
        <div v-else-if="letter.error" class="v4-err">信件加载失败：{{ letter.error }}</div>
        <pre v-else class="v4-err" style="white-space:pre-wrap">{{ letterText }}</pre>
      </section>
    </div>
    <PanelTabbar />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { storeToRefs } from 'pinia'
import { usePanelStore } from '@/stores/panel'
import { useUserStore } from '@/stores/user'
import PanelTabbar from '@/components/panel/PanelTabbar.vue'

const panel = usePanelStore()
const user = useUserStore()
const { today, parcels, pulse, letter } = storeToRefs(panel)

onMounted(() => {
  void panel.loadToday()
  void panel.loadParcels()
  void panel.loadPulse()
  void panel.loadLetter()
})

const dateLabel = computed(() => {
  const d = new Date()
  return `${d.getMonth() + 1}.${d.getDate()}`
})
const greeting = computed(() => {
  const h = new Date().getHours()
  if (h < 5) return '夜深了'
  if (h < 9) return '早上好'
  if (h < 12) return '上午好'
  if (h < 14) return '中午好'
  if (h < 18) return '下午好'
  return '晚上好'
})
const username = computed(() => (user.user as Record<string, unknown> | null)?.nickname as string || '探险家')
const parcelCount = computed(() => (today.value.data as Record<string, unknown> | null)?.parcel_count ?? '?')
const streak = computed(() => {
  const v = (today.value.data as Record<string, unknown> | null)?.streak_days
  return typeof v === 'number' ? v : null
})
const parcelText = computed(() => JSON.stringify(parcels.value.data, null, 2))
const pulseText = computed(() => JSON.stringify(pulse.value.data, null, 2))
const letterText = computed(() => JSON.stringify(letter.value.data, null, 2))
</script>
