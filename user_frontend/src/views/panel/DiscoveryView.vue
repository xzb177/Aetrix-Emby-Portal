<template>
  <div class="v4-scope">
    <div class="v4-page">
      <!-- 日志头：问候 + 封存件数 + 连签 -->
      <header class="v4-loghead">
        <span class="v4-stamp">探险日志 · {{ dateLabel }}</span>
        <h1>{{ greeting }}，{{ nickname }}</h1>
        <p v-if="today.data && parcelTotal !== '?'">今天片库里有 {{ parcelTotal }} 件新封存，等你拆。</p>
        <div v-else-if="today.loading" class="v4-skeleton" style="height:28px;width:70%;margin-top:10px"></div>
        <div v-else-if="today.error" class="v4-err" style="margin-top:10px">今日概览加载失败：{{ today.error }}<br><button class="v4-retry" @click="() => panel.loadToday(true)">重试</button></div>
        <div v-if="streak != null && streak > 0" class="v4-fire">🔥 连签 <b>{{ streak }}</b> 天 · 明天有惊喜</div>
      </header>

      <!-- 今日封存：盲盒 -->
      <section class="v4-sec">
        <div class="v4-sec-title">📦 今日封存</div>
        <div class="v4-sec-sub">新入库的片子，先拆为敬 —— 点一下开封</div>
        <div v-if="parcels.loading" class="v4-skeleton" style="height:132px"></div>
        <div v-else-if="parcels.error" class="v4-err">封存加载失败：{{ parcels.error }}<br><button class="v4-retry" @click="() => panel.loadParcels(true)">重试</button></div>
        <div v-else-if="parcelList.length === 0" class="v4-empty">今天还没有新封存，明天再来拆 📦</div>
        <div v-else class="v4-parcels">
          <ParcelBox
            v-for="p in parcelList"
            :key="String(p.id)"
            :parcel="p"
            @unseal="onUnseal"
          />
        </div>
      </section>

      <!-- 服务器脉搏 -->
      <section class="v4-sec">
        <div class="v4-sec-title">💓 服务器脉搏</div>
        <div class="v4-sec-sub">它现在活着，在干什么 —— 实时</div>
      </section>
      <div class="v4-pulse-box">
        <div v-if="!pulse.error" class="v4-pulse-row">
          <span class="v4-pulse-tag">● LIVE · 后台任务</span>
          <span class="v4-pulse-dot"></span>
        </div>
        <div v-if="pulse.loading" class="v4-skeleton" style="height:98px"></div>
        <template v-else-if="!pulse.error">
          <EcgCanvas />
          <PulseActivity :items="activityTexts" />
        </template>
      </div>
      <div v-if="pulse.error" class="v4-sec"><div class="v4-err">脉搏加载失败：{{ pulse.error }}<br><button class="v4-retry" @click="() => panel.loadPulse(true)">重试</button></div></div>

      <!-- 每日一封 -->
      <section class="v4-sec" style="padding-bottom:20px">
        <div class="v4-sec-title">✉️ 每日一封</div>
        <div class="v4-sec-sub">昨天的你，留了一封信 —— 点开看看</div>
        <div v-if="letter.loading" class="v4-skeleton" style="height:120px"></div>
        <div v-else-if="letter.error" class="v4-err">信件加载失败：{{ letter.error }}<br><button class="v4-retry" @click="() => panel.loadLetter(true)">重试</button></div>
        <WaxLetter v-else-if="hasLetterData" :letter="letterData" />
        <div v-else class="v4-empty">昨天的信还在路上，先去看片吧</div>
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
import { useToast } from '@/composables/useToast'
import PanelTabbar from '@/components/panel/PanelTabbar.vue'
import ParcelBox from '@/components/panel/ParcelBox.vue'
import EcgCanvas from '@/components/panel/EcgCanvas.vue'
import PulseActivity from '@/components/panel/PulseActivity.vue'
import WaxLetter from '@/components/panel/WaxLetter.vue'
import type { ParcelItem, ParcelsData } from '@/composables/usePanelApi'

const panel = usePanelStore()
const user = useUserStore()
const { today, parcels, pulse, letter } = storeToRefs(panel)

onMounted(() => {
  void panel.loadToday()
  void panel.loadParcels()
  void panel.loadPulse()
  void panel.loadLetter()
})

/* ---------- 日志头 ---------- */
const todayData = computed(() => (today.value.data || {}) as Record<string, unknown>)
const dateLabel = computed(() => {
  const raw = todayData.value.date as string | undefined
  if (raw) {
    const d = new Date(raw)
    if (!Number.isNaN(d.getTime())) return `${d.getMonth() + 1}.${d.getDate()}`
  }
  const d = new Date()
  return `${d.getMonth() + 1}.${d.getDate()}`
})
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
const parcelTotal = computed(() => {
  const v = todayData.value.parcels_total
  return typeof v === 'number' ? v : '?'
})
const streak = computed(() => {
  const v = todayData.value.streak
  return typeof v === 'number' ? v : null
})

/* ---------- 今日封存 ---------- */
const parcelList = computed((): ParcelItem[] => {
  const d = parcels.value.data as ParcelsData | null
  return Array.isArray(d?.parcels) ? d.parcels : []
})
async function onUnseal(id: string | number) {
  try {
    await panel.unseal(id)
  } catch (e) {
    // 失败不再静默：toast 告诉用户具体原因
    useToast().error(e instanceof Error ? e.message : '拆封失败，稍后再试')
  }
}

/* ---------- 服务器脉搏 ---------- */
/** 防御：后端曾把 Python dict 直接 str() 后塞进 text（如 {'level': 'down', ...}），
 *  这里把 message 抠出来，避免原始 dict 显示给用户。后端已修（panel.py），
 *  此为兜底（处理旧缓存/其他漏网路径）。 */
function cleanActivityText(t: string): string {
  const s = t.trim()
  if (s.startsWith('{') && /['"]message['"]/.test(s)) {
    const m1 = s.match(/'message'\s*:\s*'((?:[^'\\]|\\.)*)'/)
    if (m1) return m1[1].replace(/\\'/g, "'")
    const m2 = s.match(/"message"\s*:\s*"((?:[^"\\]|\\.)*)"/)
    if (m2) return m2[1].replace(/\\"/g, '"')
  }
  return s
}
const activityTexts = computed((): string[] => {
  const d = (pulse.value.data || {}) as Record<string, unknown>
  const acts = d.activities as Array<{ text?: string }> | undefined
  if (Array.isArray(acts)) {
    const texts = acts.map((a) => cleanActivityText(String(a?.text || ''))).filter(Boolean)
    if (texts.length) return texts
  }
  return ['正在感知服务器…']
})

/* ---------- 每日一封 ---------- */
const letterData = computed(() => (letter.value.data || {}) as Record<string, unknown>)
/** 空对象 = 后端没给信，不渲染"伪造"信封，走空态 */
const hasLetterData = computed(() => Object.keys(letterData.value).length > 0)
</script>

<style scoped>
/* 封存包裹行：取自设计稿 .parcels */
.v4-parcels {
  display: flex;
  gap: 12px;
}
/* 脉搏黑盒：取自设计稿 .pulse-box */
.v4-pulse-box {
  background: var(--ink);
  border-radius: 14px;
  padding: 16px;
  margin: 0 20px;
  color: #EDE6D6;
  position: relative;
  overflow: hidden;
}
.v4-pulse-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}
.v4-pulse-tag {
  font-size: 11px;
  letter-spacing: 2px;
  color: #9BE08A;
}
.v4-pulse-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #9BE08A;
  animation: v4-blink 1.6s infinite;
}
@keyframes v4-blink {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.25; }
}
</style>
