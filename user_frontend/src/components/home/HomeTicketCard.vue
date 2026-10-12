<script setup lang="ts">
import { RouterLink } from 'vue-router'
import type { Component } from 'vue'

export interface AssetCard {
  key: string
  to: string
  icon: Component
  title: string
  value: string
  unit: string
  progress: number | null
  desc: string
  note: string
  short: string
  footer: string
  badge: string | null
  hot: boolean
}

defineProps<{
  loading: boolean
  assetCards: AssetCard[]
  memberFooter: string
  planName: string
}>()
</script>

<template>
  <section v-if="loading" class="ticket" aria-hidden="true">
    <div class="ticket-main">
      <div v-for="i in 3" :key="i" class="ticket-stat">
        <span class="au-skeleton sk-stat-label"></span>
        <span class="au-skeleton sk-stat-value"></span>
        <span class="au-skeleton sk-stat-desc"></span>
      </div>
    </div>
    <div class="ticket-stub">
      <span class="au-skeleton sk-stub-btn"></span>
    </div>
  </section>
  <section v-else class="ticket au-anim-up" aria-label="我的资产">
    <div class="ticket-main">
      <component
        :is="c.to ? RouterLink : 'div'"
        v-for="c in assetCards"
        :key="c.key"
        :to="c.to || undefined"
        class="ticket-stat"
        :class="{ 'is-static': !c.to }"
      >
        <span class="stat-label">
          <component :is="c.icon" :size="14" class="stat-icon" />
          {{ c.title }}
          <span v-if="c.badge" class="stat-badge" :class="{ hot: c.hot }">{{ c.badge }}</span>
        </span>
        <span class="stat-value-row">
          <span class="stat-value">{{ c.value }}</span>
          <span v-if="c.unit" class="stat-unit">{{ c.unit }}</span>
        </span>
        <span class="stat-foot">
          <span
            v-if="c.progress !== null"
            class="stat-progress"
            :title="`套餐周期已过 ${c.progress}%`"
          >
            <span class="stat-progress-fill" :style="{ transform: `scaleX(${c.progress / 100})` }"></span>
          </span>
          <span v-else-if="c.short" class="stat-short">{{ c.short }}</span>
        </span>
        <span class="stat-desc">{{ c.desc }}</span>
        <span class="stat-note">{{ c.note }}</span>
      </component>
    </div>
    <div class="ticket-stub">
      <span class="stub-admit" aria-hidden="true">
        ADMIT ONE<span v-if="planName" class="stub-plan"><i> · </i>{{ planName }}</span>
      </span>
      <RouterLink to="/store" class="au-btn au-btn-primary au-btn-sm stub-btn">
        {{ memberFooter }}
      </RouterLink>
    </div>
  </section>
</template>

<style scoped>
/* 票根卡样式从 HomeView 迁移，保持视觉一致 */
.ticket {
  display: grid;
  grid-template-columns: 1fr auto;
  background: var(--au-surface);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-md);
  overflow: hidden;
}
.ticket-main {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
}
.ticket-stat {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 16px 18px;
  min-width: 0;
  color: inherit;
  text-decoration: none;
}
.ticket-stat.is-static { cursor: default; }
.stat-label {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--au-text-3);
}
.stat-icon { flex-shrink: 0; }
.stat-badge {
  margin-left: auto;
  padding: 1px 6px;
  border-radius: 999px;
  font-size: 10px;
  background: var(--au-surface-2);
}
.stat-badge.hot { background: var(--au-warning-soft); color: var(--au-warning); }
.stat-value-row { display: flex; align-items: baseline; gap: 4px; }
.stat-value { font-size: 28px; font-weight: 700; line-height: 1.1; }
.stat-unit { font-size: 13px; color: var(--au-text-3); }
.stat-foot { height: 18px; display: flex; align-items: center; }
.stat-progress {
  flex: 1;
  height: 4px;
  background: var(--au-surface-2);
  border-radius: 2px;
  overflow: hidden;
}
.stat-progress-fill {
  display: block;
  height: 100%;
  width: 100%;
  background: var(--au-primary);
  transform-origin: left;
}
.stat-short { font-size: 12px; color: var(--au-text-2); }
.stat-desc, .stat-note { font-size: 12px; color: var(--au-text-3); }
.ticket-stub {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 12px 16px;
  border-left: 1px dashed var(--au-border);
  min-width: 120px;
}
.stub-admit { font-size: 10px; letter-spacing: 0.12em; color: var(--au-text-3); }
@media (--au-bp-md) {
  .ticket { grid-template-columns: 1fr; }
  .ticket-main { grid-template-columns: 1fr; }
  .ticket-stub { border-left: none; border-top: 1px dashed var(--au-border); flex-direction: row; }
  .stub-btn {
    flex-shrink: 0;
    min-width: 5rem;
  }
  .stub-plan { letter-spacing: 0.24em; }
  .stub-plan i { display: inline; }
}

/* 从 HomeView 迁移（#706 抽取遗漏，home-refactor 修复）：
   存根 CTA 与骨架屏类在 template 中使用，定义残留在 HomeView scoped（scoped 不穿透 → 样式失效）。 */
.stub-btn {
  min-width: 6rem;
}

/* 紧凑琥珀按钮：盖过 mobile.css 对所有 a 的 44px 最小高度（票根里它不是唯一的点按目标——
   整张票面三格都可点） */
.ticket-stub .stub-btn {
  min-height: 0;
  height: 36px;
  padding: 0 1.25rem;
}

/* 宽屏票根是竖条：ADMIT ONE 一行、套餐名另起一行；窄屏横排时才用「 · 」连成一行 */
.stub-plan {
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 100%;
  letter-spacing: 0.12em;
}
.stub-plan i { display: none; font-style: normal; }

/* 骨架 */
.sk-stat-label { display: block; width: 4rem; height: 14px; }
.sk-stat-value { display: block; width: 5.5rem; height: 36px; margin: 0.25rem 0; }
.sk-stat-desc { display: block; width: 80%; height: 13px; }
.sk-stub-btn { display: block; width: 6rem; height: 32px; }
</style>
