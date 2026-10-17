<template>
  <div class="v4-scope">
    <div class="v4-page">
      <!-- 顶栏 -->
      <div class="v4-col-top">
        <h1><span aria-hidden="true">🏅</span> 收藏家</h1>
        <span v-if="collector.loading" class="v4-skeleton" style="height:24px;width:110px;border-radius:14px"></span>
        <span v-else-if="!collector.error" class="v4-lv">{{ levelText }}</span>
      </div>

      <!-- 顶层错误卡：失败时各区收起，只留这一张 -->
      <div v-if="collector.error" class="v4-sec">
        <div class="v4-err" role="alert">收藏家没加载出来：{{ collector.error }}<br><button class="v4-retry" :disabled="collector.loading" @click="retryCollector">{{ collector.loading ? '正在重试…' : '重试' }}</button></div>
      </div>

      <!-- 收藏家卡 -->
      <div v-if="!collector.error" class="v4-idcard">
        <span class="v4-idcard-mark" aria-hidden="true">收藏家</span>
        <div v-if="collector.loading" class="v4-skeleton" style="height:110px"></div>
        <template v-else>
          <div class="v4-id-name">{{ nickname }}</div>
          <!-- R41：口味数据没拿到（非加载态的静默失败）时给重试入口，调 loadCollector(true) 强制刷新 -->
          <div class="v4-id-title">{{ tasteTitle }}<button v-if="!tastesRaw" class="v4-retry" @click="retryCollector">重试</button></div>
          <div class="v4-id-stats">
            <div><b>{{ points }}</b><span>能量</span></div>
            <div><b>{{ watchHoursText }}</b><span>累计观影</span></div>
            <div><b>{{ streakText }}</b><span>连签天数</span></div>
            <div><b>{{ achievementCount }}</b><span>成就</span></div>
          </div>
        </template>
      </div>

      <!-- 新解锁横幅（R27：可关闭；已读记 sessionStorage，同会话不再弹；R28：加载失败时不展示，避免旧数据误导） -->
      <div v-if="hasNewUnlock && !unlockBannerDismissed && !collector.error" class="v4-sec">
        <div class="v4-fresh" role="status">
          <span><span aria-hidden="true">🎉</span> {{ newUnlockNames.length ? '新成就解锁：' + newUnlockNames.join('、') : '有新成就解锁，去看看吧' }}</span>
          <button class="v4-fresh-close" aria-label="关闭新成就横幅" @click="dismissUnlockBanner">✕</button>
        </div>
      </div>

      <!-- 口味护照 -->
      <section v-if="!collector.error" class="v4-sec">
        <div class="v4-sec-title"><span aria-hidden="true">🛂</span> 口味护照</div>
        <div class="v4-sec-sub">看够一个类型，就盖一个章 —— 集满 12 枚有惊喜</div>
        <!-- loading 契约直通子组件，父级不再用 v-else 抢占骨架分支 -->
        <TastePassport :raw="tastesRaw" :loading="collector.loading" @retry="retryCollector" />
      </section>

      <!-- 馆灵养成 -->
      <section v-if="!collector.error" class="v4-sec" style="padding-bottom:20px">
        <div class="v4-sec-title"><span aria-hidden="true">🥚</span> 馆灵养成</div>
        <div class="v4-sec-sub">你看的每一分钟，都在喂养它</div>
        <div v-if="collector.loading" class="v4-skeleton" style="height:220px"></div>
        <PetCard v-else-if="pet" :pet="pet" />
        <div v-else class="v4-empty">馆灵还在路上，多看片把它养出来</div>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { usePanelStore } from '@/stores/panel'
import { useUserStore } from '@/stores/user'
import TastePassport from '@/components/panel/TastePassport.vue'
import PetCard from '@/components/panel/PetCard.vue'
import type { Achievement, CollectorData, PetData, TasteStamp } from '@/composables/usePanelApi'

const panel = usePanelStore()
const user = useUserStore()
const { collector } = storeToRefs(panel)

onMounted(() => {
  void panel.loadCollector()
})

/** 错误卡重试 */
function retryCollector() {
  void panel.loadCollector(true)
}

/* ---------- 数据解包 ---------- */
const data = computed((): CollectorData => (collector.value.data || {}) as CollectorData)

const nickname = computed(() => {
  const n = data.value.nickname as string | undefined
  if (n) return n
  const u = user.user as Record<string, unknown> | null
  return (u?.nickname as string) || (u?.username as string) || '探险家'
})

const points = computed(() => data.value.points ?? '–')
const watchHoursText = computed(() => {
  const h = data.value.watch_hours
  if (typeof h !== 'number') return '–'
  return h >= 1 ? `${Math.round(h)}小时` : `${Math.round(h * 60)}分钟`
})
const streakText = computed(() => data.value.streak ?? '–')
const achievementCount = computed(() => data.value.achievement_count ?? '–')

/* ---------- 等级徽章：按成就数给称号 ----------
   R34：阈值改为 API 驱动——后端若在 /api/panel/collector 返回 level_tiers
   （[{min: 成就数下限, title: 称号}]，按 min 从高到低），前端直接采用；
   无该字段时用本地兜底（与当前线上值一致），避免无称号。 */
interface CollectorLevelTier { min: number; title: string }
const FALLBACK_LEVEL_TIERS: CollectorLevelTier[] = [
  { min: 30, title: '传奇藏家' },
  { min: 20, title: '大藏家' },
  { min: 10, title: '资深藏家' },
  { min: 5, title: '藏家' },
  { min: 1, title: '新锐' },
]
const BASE_TITLE = '见习'

const levelTiers = computed((): CollectorLevelTier[] => {
  const raw = data.value.level_tiers
  if (Array.isArray(raw) && raw.length > 0 && raw.every(
    (t) => t && typeof (t as CollectorLevelTier).min === 'number' && typeof (t as CollectorLevelTier).title === 'string',
  )) {
    return [...raw as CollectorLevelTier[]].sort((a, b) => b.min - a.min)
  }
  return FALLBACK_LEVEL_TIERS
})

const levelText = computed(() => {
  // achievement_count 缺失时与统计区口径统一：显示 '–'，不按 0 假装 Lv.1
  if (typeof data.value.achievement_count !== 'number') return '–'
  const c = data.value.achievement_count
  const tiers = levelTiers.value
  for (let i = 0; i < tiers.length; i++) {
    // R53B-31：等级号走固定映射（第 1 档 → Lv.6，逐档递减），不再依赖 tiers.length
    if (c >= tiers[i].min) return `Lv.${Math.max(1, 6 - i)} · ${tiers[i].title}`
  }
  return `Lv.1 · ${BASE_TITLE}`
})

/* ---------- 口味标题：取已集的前两个 ---------- */
const tastesRaw = computed((): Record<string, TasteStamp> | undefined => {
  const t = data.value.tastes as Record<string, TasteStamp> | undefined
  return t && typeof t === 'object' ? t : undefined
})
const tasteTitle = computed(() => {
  const t = tastesRaw.value
  if (!t) return '口味数据没拿到'
  const got = Object.values(t).filter((s) => s?.stamped).map((s) => s.name)
  if (got.length === 0) return '还没有集到口味章，去看片吧'
  return `口味：${got.slice(0, 2).join(' / ')}${got.length > 2 ? ` 等 ${got.length} 种` : ''}`
})

/* ---------- 成就 ---------- */
const achievements = computed((): Achievement[] => {
  const a = data.value.achievements
  return Array.isArray(a) ? a : []
})
const newUnlockNames = computed((): string[] => {
  const keys = data.value.new_unlocks
  if (!Array.isArray(keys) || keys.length === 0) return []
  const byKey = new Map(achievements.value.map((a) => [a.key, a.name]))
  // P3-19：找不到名字的 key 直接过滤；全部找不到时由模板走整句兜底文案
  return keys.map((k) => byKey.get(String(k))).filter((n): n is string => !!n)
})

/** 有 new_unlocks 就展示横幅（即使名字一个都没匹配上） */
const hasNewUnlock = computed((): boolean => {
  const keys = data.value.new_unlocks
  return Array.isArray(keys) && keys.length > 0
})

/* ---------- 新解锁横幅关闭（R27：已读记 sessionStorage，同会话不再弹） ---------- */
const UNLOCK_BANNER_KEY = 'aetrix_unlock_banner_dismissed_v1'
const unlockBannerDismissed = ref(
  (() => { try { return sessionStorage.getItem(UNLOCK_BANNER_KEY) === '1' } catch { return false } })(),
)
function dismissUnlockBanner() {
  unlockBannerDismissed.value = true
  try { sessionStorage.setItem(UNLOCK_BANNER_KEY, '1') } catch { /* 记不住就下次还弹，不打扰 */ }
}

/* ---------- 馆灵 ---------- */
const pet = computed((): PetData | null => {
  const p = data.value.pet as PetData | undefined
  if (!p || typeof p !== 'object' || !Array.isArray(p.stages) || p.stages.length === 0) return null
  return p
})
</script>

<style scoped>
/* 取自设计稿 .top / .lv / .idcard / .stats */
.v4-col-top {
  padding: 26px 20px 6px;
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.v4-col-top h1 {
  font-size: 20px;
  font-weight: 800;
  letter-spacing: 2px;
  margin: 0;
}
.v4-lv {
  font-size: 11px;
  background: var(--ink);
  color: var(--paper); /* R27：原 #f6f1e7 = --paper */
  padding: 5px 12px;
  border-radius: 14px;
  letter-spacing: 1px;
  white-space: nowrap;
}
.v4-idcard {
  margin: 12px 20px 0;
  background: var(--paper-card);
  border: 1.5px solid var(--ink);
  border-radius: 14px;
  padding: 18px;
  position: relative;
  overflow: hidden;
}
.v4-idcard-mark {
  position: absolute;
  right: -8px;
  top: 8px;
  font-size: 44px;
  font-weight: 900;
  color: rgba(42, 38, 32, 0.05);
  letter-spacing: 4px;
  transform: rotate(8deg);
  pointer-events: none;
}
.v4-id-name {
  font-size: 22px;
  font-weight: 800;
}
.v4-id-title {
  font-size: 12px;
  color: var(--ink2);
  margin-top: 2px;
}
.v4-id-stats {
  display: flex;
  margin-top: 14px;
  border-top: 1px dotted var(--line);
  padding-top: 12px;
}
.v4-id-stats div {
  flex: 1;
  min-width: 0;
  text-align: center;
}
.v4-id-stats b {
  font-size: 18px;
  display: block;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.v4-id-stats span {
  font-size: 11px;
  color: var(--ink2);
}
.v4-id-stats div + div {
  border-left: 1px dotted var(--line);
}
/* 新解锁横幅 */
.v4-fresh {
  background: rgba(255, 217, 138, 0.25);
  border: 1.5px solid var(--gold);
  border-radius: var(--paper-r-lg, 12px);
  padding: 10px 14px;
  font-size: 12px;
  animation: v4-fadein 0.4s ease;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
/* P3-22：长文案 375px 下撑版，文本 span 允许截断 */
.v4-fresh > span {
  min-width: 0;
  overflow: hidden;
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}
/* R27：横幅关闭按钮（44px 点击区内嵌，不破坏纸色风格） */
.v4-fresh-close {
  flex: none;
  border: none;
  background: none;
  color: var(--ink2);
  font-size: 14px;
  min-width: 44px;
  min-height: 44px;
  margin: -10px -10px -10px 0;
  cursor: pointer;
}
@keyframes v4-fadein {
  from {
    opacity: 0;
    transform: translateY(8px);
  }
  to {
    opacity: 1;
    transform: none;
  }
}

/* R27：减少动态偏好下横幅直接出现，不淡入（本地守卫） */
@media (prefers-reduced-motion: reduce) {
  .v4-fresh {
    animation: none;
  }
}
</style>
