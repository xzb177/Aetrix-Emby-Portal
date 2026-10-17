<!-- 桌面发现页左栏 · 个人问候卡（C档第2批）
     问候两档字排 + 日期行 + 积分/活力/连签 chips，全部真实数据：
     问候/日期/昵称走 /api/panel/today；积分与连签走签到状态接口；
     活力走活力值接口（403/失败则隐藏该 chip，不编数）。 -->
<template>
  <section class="d3-card d3-hello" aria-label="个人问候">
    <span class="h1">{{ greeting }}，</span>
    <span class="h2">{{ nickname }}</span>
    <div class="d3-date-line">{{ dateLine }}</div>
    <div class="d3-chips">
      <span v-if="points !== null" class="d3-chip">积分 <b>{{ points.toLocaleString('zh-CN') }}</b></span>
      <span v-if="vitality !== null" class="d3-chip">活力 <b>{{ vitality }}</b></span>
      <span v-if="streak !== null && streak > 0" class="d3-chip">已连签 <b>{{ streak }} 天</b></span>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { usePanelStore } from '@/stores/panel'
import { useUserStore } from '@/stores/user'
import { checkinApi, vitalityApi } from '@/api/economy'

const panel = usePanelStore()
const user = useUserStore()
const { today } = storeToRefs(panel)

const todayData = computed(() => (today.value.data || {}) as Record<string, unknown>)

const greeting = computed(() => {
  const g = todayData.value.greeting as string | undefined
  if (g) return g
  const h = new Date().getHours()
  if (h < 5) return '夜深了'
  if (h < 9) return '早上好'
  if (h < 12) return '上午好'
  if (h < 14) return '中午好'
  if (h < 18) return '下午好'
  return '晚上好'
})
const nickname = computed(() => {
  const n = todayData.value.nickname as string | undefined
  if (n) return n
  const u = user.user as Record<string, unknown> | null
  return (u?.nickname as string) || (u?.username as string) || '探险家'
})
const dateLine = computed(() => {
  const raw = todayData.value.date_label as string | undefined
  let md = ''
  const wd = todayData.value.weekday as string | undefined
  if (raw) {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(raw)
    if (m) md = `${Number(m[2])}月${Number(m[3])}日`
  }
  if (!md) {
    const d = new Date()
    md = `${d.getMonth() + 1}月${d.getDate()}日`
  }
  const parts = [md]
  if (wd) parts.push(`周${wd}`)
  const pc = todayData.value.parcel_count
  if (typeof pc === 'number') parts.push(`今天片库新增 ${pc} 条`)
  return parts.join(' · ')
})

const points = ref<number | null>(null)
const streak = ref<number | null>(null)
const vitality = ref<number | null>(null)

onMounted(() => {
  if (!user.isLoggedIn) return
  // 积分 + 连签：签到状态接口一次拿齐（与顶栏同口径）
  checkinApi.status()
    .then((st) => {
      if (typeof st.points === 'number') points.value = st.points
      if (typeof st.streak === 'number') streak.value = st.streak
    })
    .catch(() => { /* 拿不到就不显示，不打扰 */ })
  // 活力：公益服用户才有，403/失败直接隐藏
  vitalityApi.status()
    .then((st) => {
      if (typeof st.vitality === 'number') vitality.value = st.vitality
    })
    .catch(() => { /* 拿不到就不显示，不打扰 */ })
})
</script>
