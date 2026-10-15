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

      <!-- 营地地图板：纸纹底 + 虚线小径 + 手绘风分区牌，CSS 实现，不引图片资源。
           设施来自 @/config/game 注册表（只显示 enabled，按 order 排），
           与头像菜单同一份数据、顺序名字一字不差。 -->
      <section class="map-board" aria-label="营地地图">
        <div v-for="g in zoneList" :key="g.key" class="zone">
          <div class="zone-sign">
            <span class="zone-icon" aria-hidden="true">{{ g.icon }}</span>
            <span class="zone-title">{{ g.title }}</span>
            <span class="zone-sub">{{ g.sub }}</span>
          </div>
          <div class="zone-cards">
            <router-link
              v-for="f in g.items"
              :key="f.key"
              :to="f.path"
              class="v4-card map-card"
              :aria-label="liveBadge(f) ? `${f.name}，${liveBadge(f)}` : f.name"
            >
              <div class="map-icon" aria-hidden="true">{{ f.icon }}</div>
              <div class="map-body">
                <div class="map-name">
                  {{ f.name }}
                  <span v-if="liveBadge(f)" class="live-badge">{{ liveBadge(f) }}</span>
                </div>
                <div class="map-desc">{{ f.desc }}</div>
              </div>
              <div class="map-go" aria-hidden="true">›</div>
            </router-link>
          </div>
        </div>
      </section>
    </div>
    <PanelTabbar />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import PanelTabbar from '@/components/panel/PanelTabbar.vue'
import CampGuide from '@/components/game/CampGuide.vue'
import {
  gameHubConfig as hub,
  gameFacilities,
  gameFacilityGroups,
  type GameFacility,
} from '@/config/game'
import { checkinApi, vitalityApi } from '@/api/economy'
import { messageApi } from '@/api'
import { useUserStore } from '@/stores/user'
import { DEFAULT_STAMINA_MAX } from '@/config/stamina'

const userStore = useUserStore()

/** 4 个分区：只显示 enabled 的设施，按 order 排，空分区不渲染 */
const zoneList = computed(() =>
  gameFacilityGroups
    .map((g) => ({
      ...g,
      items: gameFacilities
        .filter((f) => f.enabled && f.group === g.key)
        .sort((a, b) => a.order - b.order),
    }))
    .filter((g) => g.items.length > 0),
)

// ---------- 今日状态条：体力 (vitalityApi) + 报到 (checkinApi)，失败静默降级 ----------
const vitality = ref<number | null>(null)
const vitalityMax = ref<number | null>(null)
const checkedToday = ref<boolean | null>(null)
const unreadCount = ref(0)

const statusText = computed(() => {
  // R45：三路 API 全失败——诚实文案，不说"元气满满"（状态条本身即报到页可点入口）
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

/** R28：三路 API 全失败标记——状态条前加小琥珀点 + aria-label，文案不动 */
const allFailed = ref(false)

/** R54-15：首屏三路请求是否已落定——用于区分「加载中」与「真实结果」 */
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

// ---------- 活卡片徽标：失败全部静默降级为静态卡 ----------
function liveBadge(f: GameFacility): string {
  if (f.key === 'checkin') {
    if (checkedToday.value === true) return '✓今日已报到'
    if (checkedToday.value === false) return '待报到'
    return ''
  }
  if (f.key === 'pigeon') {
    return unreadCount.value > 0 ? `·${unreadCount.value}封未读` : ''
  }
  return ''
}

onMounted(async () => {
  if (!userStore.isLoggedIn) return
  // R54-15：三个请求互相独立，并发执行，各自 try/catch + 超时，失败只留静态展示
  const [vitalityRes, checkinRes, unreadRes] = await Promise.all([
    withTimeout(vitalityApi.status()).catch((): null => null),
    withTimeout(checkinApi.status()).catch((): null => null),
    withTimeout(messageApi.getUnreadCount()).catch((): null => null),
  ])
  // R28：三路全失败时用户要有失败感知（弱提示，不改文案）
  allFailed.value = !vitalityRes && !checkinRes && !unreadRes
  // R54-16：字段缺省时归一化，报到状态未知保持 null（走中性文案）
  if (vitalityRes) {
    vitality.value = toNumberOrNull(vitalityRes.vitality)
    vitalityMax.value = toNumberOrNull(vitalityRes.max)
  }
  if (checkinRes) {
    const checked = checkinRes.checked_today
    checkedToday.value = typeof checked === 'boolean' ? checked : null
  }
  if (unreadRes) {
    unreadCount.value = Number(unreadRes.unread_count) || 0
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
/* R28：三路 API 全失败时的弱提示——小琥珀点（文案不动，最小侵入） */
.status-dot.stale { background: var(--gold, #c8862a); }
.status-strip:focus-visible {
  outline: 2px solid var(--red);
  outline-offset: 2px;
}

/* 营地地图板：纸纹底 */
.map-board {
  margin-top: 16px;
  padding: 4px 2px 0;
  position: relative;
}

.zone {
  margin-bottom: 18px;
  position: relative;
}

/* 手绘风分区牌：微微歪斜的牛皮纸标签 */
.zone-sign {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  /* R28：标题过长时 375px 下不溢出 */
  /* R54-18：再扣掉 margin-left 4px，避免 margin box 超出 .map-board */
  max-width: calc(100% - 4px);
  background: #e9d3a3;
  border: 1.5px solid var(--line);
  border-radius: 4px 14px 4px 14px;
  padding: 6px 14px;
  transform: rotate(-1.2deg);
  box-shadow: 1px 2px 0 rgba(0, 0, 0, 0.08);
  margin-bottom: 10px;
  margin-left: 4px;
}
.zone-icon {
  font-size: 18px;
  line-height: 1;
}
.zone-title {
  font-size: 14px;
  font-weight: 800;
  letter-spacing: 2px;
  /* R28：配合 .zone-sign 的 max-width，超长标题省略不溢出 */
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.zone-sub {
  font-size: 11px;
  color: var(--paper-ink);
  border-left: 1px solid var(--line);
  padding-left: 6px;
  margin-left: 2px;
}

/* 虚线小径：卡片之间的手绘小径 */
.zone-cards {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding-left: 10px;
  border-left: 2px dashed var(--line);
  margin-left: 12px;
}

.map-card {
  display: flex;
  align-items: center;
  gap: 12px;
  text-decoration: none;
  color: inherit;
  transition: transform 0.12s ease;
}
.map-card:active {
  transform: scale(0.98);
}
.map-icon {
  font-size: 28px;
  line-height: 1;
  flex-shrink: 0;
  width: 44px;
  height: 44px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--paper2);
  border: 1.5px solid var(--line);
  border-radius: 50% 46% 52% 48% / 48% 52% 46% 54%;
}
.map-body {
  flex: 1;
  min-width: 0;
}
.map-name {
  font-size: 15px;
  font-weight: 800;
  letter-spacing: 1px;
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
/* 活卡片徽标：手写体小贴纸 */
.live-badge {
  font-size: 11px;
  font-weight: 800;
  color: var(--red);
  background: #fdf3e7;
  border: 1px dashed var(--red);
  border-radius: var(--paper-r-md, 10px);
  padding: 1px 8px;
  white-space: nowrap;
}
.map-desc {
  font-size: 12px;
  color: var(--ink2);
  margin-top: 2px;
  /* R27：单行 nowrap 在窄屏下截断关键信息 → 最多两行。
     line-clamp 场景下 text-overflow 不生效，不写无效声明 */
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.map-go {
  font-size: 22px;
  color: var(--ink2);
  flex-shrink: 0;
}

.map-card:focus-visible {
  outline: 2px solid var(--red);
  outline-offset: 2px;
}

/* R27：减少动态偏好下装饰动画/过渡停掉（本地守卫） */
@media (prefers-reduced-motion: reduce) {
  .map-card { animation: none; transition: none; }
  /* R41：按压缩放的 :active 动效在减弱动态下也关闭 */
  .map-card:active { transform: none; }
}
</style>
