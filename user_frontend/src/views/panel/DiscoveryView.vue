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
        <p v-else-if="today.data">今天的新封存数还没算出来 —— 看看下面的货架上有啥。</p>
        <div v-if="streak != null && streak > 0" class="v4-fire">🔥 连签 <b>{{ streak }}</b> 天 · 明天有惊喜</div>
      </header>

      <!-- 电影票卡：积分 / 订阅 / 观影数据（发现页最显眼的位置） -->
      <div v-if="ticketLoadError && !ticketLoading" class="v4-sec">
        <div class="v4-err">票卡信息没加载出来，点下面重试<br><button class="v4-retry" @click="() => loadTicket()">重试</button></div>
      </div>
      <TicketCard
        v-else
        :loading="ticketLoading"
        :asset-cards="ticketCards"
        :member-footer="ticketFooter"
        :plan-name="ticketPlan"
      />

      <!-- 今日封存：盲盒 -->
      <section class="v4-sec">
        <div class="v4-sec-title">📦 今日封存</div>
        <div class="v4-sec-sub">新入库的片子，先拆为敬 —— 点一下开封</div>
        <div v-if="parcels.loading" class="v4-skel-row">
          <div class="v4-skeleton" style="width:132px;height:216px;flex:0 0 auto"></div>
          <div class="v4-skeleton" style="width:132px;height:216px;flex:0 0 auto"></div>
          <div class="v4-skeleton" style="width:132px;height:216px;flex:0 0 auto"></div>
        </div>
        <div v-else-if="parcels.error" class="v4-err">封存加载失败：{{ parcels.error }}<br><button class="v4-retry" @click="() => panel.loadParcels(true)">重试</button></div>
        <div v-else-if="parcelList.length === 0" class="v4-empty">今天还没有新封存，明天再来拆 📦</div>
        <div v-else ref="parcelsEl" class="v4-parcels">
          <ParcelBox
            v-for="p in parcelList"
            :key="String(p.id)"
            :parcel="p"
            :busy="unsealingIds.has(String(p.id))"
            @unseal="onUnseal"
          />
        </div>
      </section>

      <!-- 服务器脉搏 -->
      <section class="v4-sec">
        <div class="v4-sec-title">💓 服务器脉搏</div>
        <div class="v4-sec-sub">它现在活着，在干什么 —— 实时</div>
      </section>
      <!-- pulse.error 时整盒不渲染，避免留黑色空矩形（错误态在下面统一渲染） -->
      <div v-if="!pulse.error" class="v4-pulse-box">
        <div class="v4-pulse-row">
          <span class="v4-pulse-tag">● 近况 · 服务器在忙什么</span>
          <span class="v4-pulse-dot"></span>
        </div>
        <!-- R38 P3-13：骨架 112px 与真实内容（EcgCanvas 64px + PulseActivity min-height:40px + margin-top:8px）同高，避免切屏跳动 -->
        <div v-if="pulse.loading" class="v4-skeleton" style="height:112px"></div>
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
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { usePanelStore } from '@/stores/panel'
import { useUserStore } from '@/stores/user'
import { useToast } from '@/composables/useToast'
import PanelTabbar from '@/components/panel/PanelTabbar.vue'
import ParcelBox from '@/components/panel/ParcelBox.vue'
import EcgCanvas from '@/components/panel/EcgCanvas.vue'
import PulseActivity from '@/components/panel/PulseActivity.vue'
import WaxLetter from '@/components/panel/WaxLetter.vue'
import TicketCard from '@/components/panel/TicketCard.vue'
import { useTicketCard } from '@/composables/useTicketCard'
import { humanizeError, type ParcelItem, type ParcelsData } from '@/composables/usePanelApi'

const panel = usePanelStore()
const user = useUserStore()
const { today, parcels, pulse, letter } = storeToRefs(panel)
// 电影票卡：积分 / 订阅 / 观影数据（原旧首页 HomeView，已删除，逻辑见 useTicketCard）
const ticket = useTicketCard()
// 顶层解构：模板里直接用，ref 自动解包
const {
  loading: ticketLoading,
  loadError: ticketLoadError,
  assetCards: ticketCards,
  memberFooter: ticketFooter,
  planName: ticketPlan,
  load: loadTicket,
} = ticket

onMounted(() => {
  // perf 2026-10-13：合并为 1 次请求（4 个 RTT → 1 个 RTT）
  void panel.loadHome()
  void loadTicket()
  updateScrollHint()
  window.addEventListener('resize', updateScrollHint)
})
onUnmounted(() => {
  window.removeEventListener('resize', updateScrollHint)
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
/* R29：货架右侧渐隐只在有溢出可滑时显示（照 EcoGarden can-scroll 模式） */
const parcelsEl = ref<HTMLElement | null>(null)
function updateScrollHint() {
  const el = parcelsEl.value
  if (!el) return
  el.classList.toggle('can-scroll', el.scrollWidth > el.clientWidth + 1)
}
watch(parcelList, () => requestAnimationFrame(updateScrollHint))
/* 拆封中 id 集合：busy 与请求生命周期绑定（请求完成/失败才释放），替代旧的固定 600ms 释放 */
const unsealingIds = ref(new Set<string>())
async function onUnseal(id: string | number) {
  const key = String(id)
  if (unsealingIds.value.has(key)) return
  unsealingIds.value.add(key)
  try {
    await panel.unseal(id)
  } catch (e) {
    // 失败不再静默：toast 告诉用户具体原因
    useToast().error(humanizeError(e))
  } finally {
    unsealingIds.value.delete(key)
  }
}

/* ---------- 服务器脉搏 ---------- */
/** 防御：后端曾把 Python dict 直接 str() 后塞进 text（如 {'level': 'down', ...}），
 *  这里把 message 抠出来，避免原始 dict 显示给用户。后端已修（panel.py），
 *  此为兜底（处理旧缓存/其他漏网路径）。 */
function cleanActivityText(t: string): string {
  const s = t.trim()
  if (s.startsWith('{')) {
    if (/['"]message['"]/.test(s)) {
      const m1 = s.match(/'message'\s*:\s*'((?:[^'\\]|\\.)*)'/)
      if (m1) return m1[1].replace(/\\'/g, "'")
      const m2 = s.match(/"message"\s*:\s*"((?:[^"\\]|\\.)*)"/)
      if (m2) return m2[1].replace(/\\"/g, '"')
    }
    // 无 message 键的结构化残留：不暴露原始结构
    return '服务器刚才忙去了，这条动态没写清楚'
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
  return ['暂无动态']
})

/* ---------- 每日一封 ---------- */
const letterData = computed(() => (letter.value.data || {}) as Record<string, unknown>)
/** 空对象 = 后端没给信，不渲染"伪造"信封，走空态 */
const hasLetterData = computed(() => Object.keys(letterData.value).length > 0)
</script>

<style scoped>
/* 封存包裹行：横向滑动货架（见下方 .v4-parcels） */
/* 封存包裹行：横向滑动货架（单行横滑 + scroll-snap，行尾露一截提示可滑） */
.v4-parcels {
  display: flex;
  gap: 12px;
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  scroll-snap-type: x proximity;
  margin: 0 -20px;
  padding: 4px 36px 12px 20px;
  scrollbar-width: none;
}
/* R29：右侧渐隐只在货架有溢出可横滑时显示，无溢出不误导 */
.v4-parcels.can-scroll {
  -webkit-mask-image: linear-gradient(90deg, #000 0, #000 calc(100% - 44px), transparent 100%);
  mask-image: linear-gradient(90deg, #000 0, #000 calc(100% - 44px), transparent 100%);
}
.v4-parcels::-webkit-scrollbar { display: none; }
/* R29：封存骨架屏与横滑货架同形态（3 个 132×216 卡形横排） */
.v4-skel-row { display: flex; gap: 12px; overflow: hidden; }
/* 脉搏黑盒：取自设计稿 .pulse-box */
.v4-pulse-box {
  background: var(--ink);
  border-radius: 14px;
  padding: 16px;
  margin: 0 20px;
  color: var(--paper2); /* R27：原 #EDE6D6，近似 --paper2 #EFE7D6，收敛 */
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
  color: var(--pulse-green);
}
.v4-pulse-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--pulse-green);
  animation: v4-blink 1.6s infinite;
}
@keyframes v4-blink {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.25; }
}

/* R27：减少动态偏好下常亮不闪 */
@media (prefers-reduced-motion: reduce) {
  .v4-pulse-dot {
    animation: none;
    opacity: 1;
  }
}
</style>
