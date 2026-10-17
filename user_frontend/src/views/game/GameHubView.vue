<template>
  <div class="v4-scope">
    <div class="v4-page">
      <!-- 日志头 -->
      <header class="v4-loghead">
        <span class="v4-stamp">{{ hub.stamp }}</span>
        <h1>{{ hub.title }}</h1>
        <p>{{ hub.subtitle }}</p>
      </header>

      <!-- 今日状态条：一句话状态（替代旧体力槽组件）。
           没报到时可点 → 跳报到页；已报到时是纯展示。
           任一 API 失败都静默降级为静态文案，不 blocking 首屏。 -->
      <router-link
        v-if="statusClickable"
        to="/game/checkin"
        class="status-strip"
        :aria-label="allFailed ? '数据未更新，去报到盖章' : '去报到盖章'"
      >
        <!-- R37 P3-7：全失败时只留琥珀点，绿色「一切正常」点不再并列 -->
        <span v-if="allFailed" class="status-dot stale" aria-hidden="true"></span>
        <span v-else class="status-dot" aria-hidden="true"></span>
        {{ statusText }}
      </router-link>
      <div v-else class="status-strip static" role="status" :aria-label="allFailed ? '数据未更新' : undefined">
        <span v-if="allFailed" class="status-dot stale" aria-hidden="true"></span>
        <span v-else class="status-dot" aria-hidden="true"></span>
        {{ statusText }}
      </div>

      <!-- 新手引导卡：首次登录进大本营才展示，跳过后不再出现 -->
      <CampGuide />

      <!-- 世界地图本体：探险日志页 = 世界地图（原 12 设施清单已整页撤掉，设施页面路由不动） -->
      <section class="game-world" aria-label="探险世界">
        <WorldMap variant="game" />
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import CampGuide from '@/components/game/CampGuide.vue'
import WorldMap from '@/components/panel/WorldMap.vue'
import { gameHubConfig as hub } from '@/config/game'
import { checkinApi, vitalityApi } from '@/api/economy'
import { useUserStore } from '@/stores/user'
import { DEFAULT_STAMINA_MAX } from '@/config/stamina'

const userStore = useUserStore()

// ---------- 今日状态条：体力 (vitalityApi) + 报到 (checkinApi)，失败静默降级 ----------
const vitality = ref<number | null>(null)
const vitalityMax = ref<number | null>(null)
const checkedToday = ref<boolean | null>(null)

const statusText = computed(() => {
  // R45：两路 API 全失败——诚实文案，不说"元气满满"（状态条本身即报到页可点入口）
  if (allFailed.value) return '营地信号不太好，数据没拉到，点我去报到 →'
  const vit = vitality.value
  const checked = checkedToday.value
  if (vit !== null && checked !== null) {
    // R41：体力上限兜底 14 收进 config/stamina.ts 的 DEFAULT_STAMINA_MAX，不再裸魔法数字
    const base = `今日体力 ${vit}/${vitalityMax.value ?? DEFAULT_STAMINA_MAX}`
    return checked ? `${base} · 已报到✓，出发吧！` : `${base} · 还没报到，点我去盖章 →`
  }
  if (vit !== null) return `今日体力 ${vit}/${vitalityMax.value ?? DEFAULT_STAMINA_MAX}`
  if (checked !== null) return checked ? '今日已报到✓，出发吧！' : '还没报到，点我去盖章 →'
  // R54-14：状态未知（未登录 / 首屏加载中 / 请求失败）时不臆断「元气满满」
  if (!userStore.isLoggedIn) return '登录后查看今日营地情况'
  if (!statusLoaded.value) return '正在看营地情况…'
  if (allFailed.value) return '营地数据没加载出来，稍后刷新看看'
  // 两个都没拿到：静态文案，照样能点去报到
  return '营地情况待确认，点我去报到 →'
})

/** 没报到（或状态未知）时状态条可点；已报到是纯展示 */
const statusClickable = computed(() => checkedToday.value !== true)

/** R28：两路 API 全失败标记——状态条前加小琥珀点 + aria-label，文案不动 */
const allFailed = ref(false)

/** R54-15：首屏两路请求是否已落定——用于区分「加载中」与「真实结果」 */
const statusLoaded = ref(false)

/** R54-15：给单个请求加超时，避免后端不响应时永久挂起 */
function withTimeout<T>(p: Promise<T>, ms = 8000): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('timeout')), ms)
    p.then(
      (v) => { clearTimeout(timer); resolve(v) },
      (e) => { clearTimeout(timer); reject(e) },
    )
  })
}

/** R54-16：字段缺省 / 非数时归一成 number | null，避免渲染 undefined */
function toNumberOrNull(v: unknown): number | null {
  const n = Number(v)
  return v == null || !Number.isFinite(n) ? null : n
}

// ---------- 活卡片徽标已随设施清单撤掉（设施页面路由不动） ----------

onMounted(async () => {
  if (!userStore.isLoggedIn) return
  // R54-15：两个请求互相独立，并发执行，各自 try/catch + 超时，失败只留静态展示
  const [vitalityRes, checkinRes] = await Promise.all([
    withTimeout(vitalityApi.status()).catch((): null => null),
    withTimeout(checkinApi.status()).catch((): null => null),
  ])
  // R28：两路全失败时用户要有失败感知（弱提示，不改文案）
  allFailed.value = !vitalityRes && !checkinRes
  // R54-16：字段缺省时归一化，报到状态未知保持 null（走中性文案）
  if (vitalityRes) {
    vitality.value = toNumberOrNull(vitalityRes.vitality)
    vitalityMax.value = toNumberOrNull(vitalityRes.max)
  }
  if (checkinRes) {
    const checked = checkinRes.checked_today
    checkedToday.value = typeof checked === 'boolean' ? checked : null
  }
  // R54-15：首屏请求已落定，状态条从「正在看营地情况…」切到真实结果
  statusLoaded.value = true
})
</script>

<style scoped>
/* 今日状态条：小纸条，一句话 */
.status-strip {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 14px 0 0;
  /* R28：纵向 padding 提到 14px，总高 ≥44px 触摸标准 */
  padding: 14px;
  background: var(--paper-card);
  border: 1.5px dashed var(--line);
  border-radius: var(--paper-r-lg, 12px);
  font-size: 13px;
  font-weight: 700;
  color: var(--ink);
  text-decoration: none;
}
.status-dot {
  width: 8px;
  height: 8px;
  border-radius: var(--paper-r-full, 50%);
  background: var(--green);
  flex-shrink: 0;
}
/* R28：两路 API 全失败时的弱提示——小琥珀点（文案不动，最小侵入） */
.status-dot.stale { background: var(--gold, #c8862a); }
.status-strip:focus-visible {
  outline: 2px solid var(--red);
  outline-offset: 2px;
}

/* 世界地图本体：内嵌 WorldMap（/game 永远显示地图） */
.game-world {
  margin-top: 16px;
  /* 底部避让固定底栏（总高 = 内容高 var(--tabbar-h) + 上下 padding 36px + 安全区）：
     滚到底时画布底边停在底栏上方留一档呼吸距，画布下部地标（留言墙/抽签亭）不再被拦截点击 */
  padding-bottom: calc(var(--tabbar-h, 56px) + 36px + env(safe-area-inset-bottom));
}

/* 桌面 /game：地图脱离 375px 窄栏全宽展开（日志头/状态条仍居中栏宽）
   居中用负 margin 而非 transform：transform 会成为内部 position:fixed 弹层的定位基准，导致弹层错位 */
@media (min-width: 1024px) {
  .game-world {
    position: relative;
    left: 50%;
    margin-left: calc(min(1500px, 94vw) * -0.5);
    width: min(1500px, 94vw);
  }
}

@media (prefers-reduced-motion: reduce) {
  .game-world { scroll-behavior: auto; }
}
</style>
