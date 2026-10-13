<template>
  <div class="v4-scope">
    <div class="v4-page">
      <!-- 顶栏 -->
      <div class="v4-col-top">
        <h1>🏅 收藏家</h1>
        <span v-if="collector.loading" class="v4-skeleton" style="height:24px;width:110px;border-radius:14px"></span>
        <span v-else-if="!collector.error" class="v4-lv">{{ levelText }}</span>
      </div>

      <!-- 顶层错误卡：失败时各区收起，只留这一张 -->
      <div v-if="collector.error" class="v4-sec">
        <div class="v4-err">收藏家没加载出来：{{ collector.error }}<br><button class="v4-retry" @click="retryCollector">重试</button></div>
      </div>

      <!-- 收藏家卡 -->
      <div v-if="!collector.error" class="v4-idcard">
        <div v-if="collector.loading" class="v4-skeleton" style="height:110px"></div>
        <template v-else>
          <div class="v4-id-name">{{ nickname }}</div>
          <div class="v4-id-title">{{ tasteTitle }}</div>
          <div class="v4-id-stats">
            <div><b>{{ points }}</b><span>积分</span></div>
            <div><b>{{ watchHoursText }}</b><span>累计观影</span></div>
            <div><b>{{ streakText }}</b><span>连签天数</span></div>
            <div><b>{{ achievementCount }}</b><span>成就</span></div>
          </div>
        </template>
      </div>

      <!-- 新解锁横幅 -->
      <div v-if="newUnlockNames.length" class="v4-sec">
        <div class="v4-fresh">🎉 新成就解锁：{{ newUnlockNames.join('、') }}</div>
      </div>

      <!-- 口味护照 -->
      <section v-if="!collector.error" class="v4-sec">
        <div class="v4-sec-title">🛂 口味护照</div>
        <div class="v4-sec-sub">看够一个类型，就盖一个章 —— 集满 12 枚有惊喜</div>
        <div v-if="collector.loading" class="v4-skeleton" style="height:200px"></div>
        <TastePassport v-else :raw="tastesRaw" />
      </section>

      <!-- 成就星图 -->
      <section v-if="!collector.error" class="v4-sec">
        <div class="v4-sec-title">✨ 成就星图</div>
        <div class="v4-sec-sub">每颗星都是一个故事 —— 点一下看看</div>
        <div v-if="collector.loading" class="v4-skeleton" style="height:230px"></div>
        <div v-else-if="achievements.length === 0" class="v4-empty">还没有点亮成就，去看片吧 ✨</div>
        <StarMap v-else :achievements="achievements" />
      </section>

      <!-- 馆灵养成 -->
      <section v-if="!collector.error" class="v4-sec" style="padding-bottom:20px">
        <div class="v4-sec-title">🥚 馆灵养成</div>
        <div class="v4-sec-sub">你看的每一分钟，都在喂养它</div>
        <div v-if="collector.loading" class="v4-skeleton" style="height:220px"></div>
        <PetCard v-else-if="pet" :pet="pet" />
        <div v-else class="v4-empty">馆灵还在路上，多看片把它养出来</div>
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
import TastePassport from '@/components/panel/TastePassport.vue'
import StarMap from '@/components/panel/StarMap.vue'
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

/* ---------- 等级徽章：按成就数给称号 ---------- */
const levelText = computed(() => {
  const c = typeof data.value.achievement_count === 'number' ? data.value.achievement_count : 0
  if (c >= 30) return 'Lv.6 · 传奇藏家'
  if (c >= 20) return 'Lv.5 · 大藏家'
  if (c >= 10) return 'Lv.4 · 资深藏家'
  if (c >= 5) return 'Lv.3 · 藏家'
  if (c >= 1) return 'Lv.2 · 新锐'
  return 'Lv.1 · 见习'
})

/* ---------- 口味标题：取已集的前两个 ---------- */
const tastesRaw = computed((): Record<string, TasteStamp> | undefined => {
  const t = data.value.tastes as Record<string, TasteStamp> | undefined
  return t && typeof t === 'object' ? t : undefined
})
const tasteTitle = computed(() => {
  const t = tastesRaw.value
  if (!t) return '口味收集中…'
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
  return keys.map((k) => byKey.get(String(k)) || '未知')
})

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
  color: #f6f1e7;
  padding: 5px 12px;
  border-radius: 14px;
  letter-spacing: 1px;
  white-space: nowrap;
}
.v4-idcard {
  margin: 12px 20px 0;
  background: #fffdf6;
  border: 1.5px solid var(--ink);
  border-radius: 14px;
  padding: 18px;
  position: relative;
  overflow: hidden;
}
.v4-idcard::before {
  content: "COLLECTOR";
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
  text-align: center;
}
.v4-id-stats b {
  font-size: 18px;
  display: block;
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
  border-radius: 12px;
  padding: 10px 14px;
  font-size: 12px;
  animation: v4-fadein 0.4s ease;
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
</style>
