<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import type { Component } from 'vue'
import './ticket-card.css' // 纸色 token：独立样式表，供 CI 令牌契约检查识别

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

const props = defineProps<{
  loading: boolean
  assetCards: AssetCard[]
  memberFooter: string
  planName: string
}>()

// 两行布局：第一行积分 + 订阅并排（观影数据不单独占一段），
// 观影数据缩成票根行中间的一行小字
const mainCards = computed(() => props.assetCards.filter((c) => c.key !== 'watch'))
const watchCard = computed(() => props.assetCards.find((c) => c.key === 'watch') || null)
const watchLine = computed(() => {
  const w = watchCard.value
  if (!w || w.value === '—') return w?.short || ''
  return `${w.value}${w.unit} · ${w.short}`
})
</script>

<template>
  <!-- v4 纸片风电影票：两行布局（积分|订阅 / 票根行），纸面 + 墨线 + 打孔虚线 + ADMIT ONE 红印章 -->
  <section v-if="loading" class="ticket" aria-hidden="true">
    <div class="ticket-main">
      <div v-for="i in 2" :key="i" class="ticket-stat">
        <span class="tp-skeleton sk-stat-label"></span>
        <span class="tp-skeleton sk-stat-value"></span>
        <span class="tp-skeleton sk-stat-desc"></span>
      </div>
    </div>
    <div class="ticket-stub">
      <span class="tp-skeleton sk-stub-stamp"></span>
      <span class="tp-skeleton sk-stub-watch"></span>
      <span class="tp-skeleton sk-stub-btn"></span>
    </div>
  </section>
  <section v-else class="ticket" aria-label="我的资产">
    <div class="ticket-main">
      <component
        :is="c.to ? RouterLink : 'div'"
        v-for="c in mainCards"
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
        <!-- 进度条直接挂在订阅数字下面 -->
        <span
          v-if="c.progress !== null"
          class="stat-progress"
          :title="`套餐周期已过 ${c.progress}%`"
        >
          <!-- 进度条用 scaleX 而不是改 width：合成器线程就能跑，不触发布局 -->
          <span class="stat-progress-fill" :style="{ transform: `scaleX(${c.progress / 100})` }"></span>
        </span>
        <span v-else-if="c.short" class="stat-short">{{ c.short }}</span>
        <!-- 订阅：套餐名 + 到期日一行小字 -->
        <span v-if="c.key === 'member'" class="stat-desc">{{ c.desc }}</span>
      </component>
    </div>
    <!-- 票根行：顶部打孔虚线 + 左右半圆缺口；印章 / 观影小字 / 续费按钮一排 -->
    <div class="ticket-stub">
      <span class="stub-admit" aria-hidden="true">
        ADMIT ONE<span v-if="planName" class="stub-plan"><i> · </i>{{ planName }}</span>
      </span>
      <span v-if="watchLine" class="stub-watch">{{ watchLine }}</span>
      <RouterLink to="/shop" class="stub-btn">
        {{ memberFooter }}
      </RouterLink>
    </div>
  </section>
</template>

<style scoped>
/* v4 纸片风电影票：token 定义在同目录 ticket-card.css（见顶部 import），
   自包含一套纸色 token，不依赖 v4-paper.css 的变量 */
.ticket {
  display: flex;
  flex-direction: column;
  background: var(--tp-paper);
  border: 1.5px solid var(--tp-ink);
  border-radius: 12px;
  box-shadow: 3px 3px 0 rgba(42, 38, 32, 0.12);
  overflow: hidden;
  color: var(--tp-ink);
}

/* 第一行：积分 | 订阅 */
.ticket-main {
  display: grid;
  grid-template-columns: 1fr 1fr;
}
.ticket-stat {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 14px 16px 12px;
  min-width: 0;
  color: inherit;
  text-decoration: none;
}
.ticket-stat + .ticket-stat {
  border-left: 1px dashed var(--tp-line);
}
.ticket-stat.is-static { cursor: default; }
a.ticket-stat:active { background: var(--tp-paper2); }
.stat-label {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  font-weight: 700;
  color: var(--tp-ink2);
  letter-spacing: 1px;
  white-space: nowrap;
}
.stat-icon { flex-shrink: 0; }
.stat-badge {
  margin-left: auto;
  padding: 1px 8px;
  border-radius: 999px;
  font-size: 10px;
  font-weight: 700;
  background: var(--tp-paper2);
  color: var(--tp-ink2);
  border: 1px solid var(--tp-line);
  white-space: nowrap;
}
.stat-badge.hot {
  background: rgba(217, 72, 43, 0.12);
  color: var(--tp-red);
  border-color: var(--tp-red);
}
.stat-value-row { display: flex; align-items: baseline; gap: 4px; }
.stat-value { font-size: 26px; font-weight: 800; line-height: 1.1; color: var(--tp-ink); }
.stat-unit { font-size: 13px; color: var(--tp-ink2); }
.stat-short { font-size: 12px; font-weight: 700; color: var(--tp-ink2); white-space: nowrap; }
.stat-progress {
  height: 6px;
  background: var(--tp-paper2);
  border: 1px solid var(--tp-line);
  border-radius: 3px;
  overflow: hidden;
  margin-top: 2px;
}
.stat-progress-fill {
  display: block;
  height: 100%;
  width: 100%;
  background: var(--tp-gold);
  transform-origin: left;
}
.stat-desc {
  font-size: 11px;
  color: var(--tp-ink2);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* 第二行票根：顶部一条打孔虚线，左右各一个半圆缺口（填充页底色 --paper）。
   缺口圆用页面底色填充——v4 纸色页底为 #F6F1E7（.v4-scope 的 --paper） */
.ticket-stub {
  position: relative;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  border-top: 2px dashed var(--tp-line);
  background: var(--tp-paper2);
}
.ticket-stub::before,
.ticket-stub::after {
  content: '';
  position: absolute;
  top: -12px;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: var(--paper, #F6F1E7);
  border: 1.5px solid var(--tp-ink);
}
.ticket-stub::before {
  left: -13px;
  clip-path: inset(0 0 0 50%);
}
.ticket-stub::after {
  right: -13px;
  clip-path: inset(0 50% 0 0);
}
/* ADMIT ONE 做成红色印章：描边 + 微转 + 宽松字距 */
.stub-admit {
  display: inline-flex;
  align-items: baseline;
  flex-shrink: 0;
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0.18em;
  color: var(--tp-red);
  border: 1.5px solid var(--tp-red);
  border-radius: 4px;
  padding: 4px 9px 4px 11px;
  transform: rotate(-2deg);
  white-space: nowrap;
  background: rgba(217, 72, 43, 0.05);
}
.stub-plan {
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 4.5rem;
  letter-spacing: 0.1em;
  font-size: 10px;
  color: var(--tp-ink2);
}
.stub-plan i { font-style: normal; }
/* 观影数据：一行小字，居中 */
.stub-watch {
  flex: 1;
  min-width: 0;
  font-size: 11px;
  color: var(--tp-ink2);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  text-align: center;
}
/* 票根按钮：v4 墨色纸片钮 */
.stub-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  min-width: 5rem;
  min-height: 36px;
  padding: 0 1.25rem;
  background: var(--tp-ink);
  color: #F6F1E7;
  font-size: 13px;
  font-weight: 700;
  border-radius: 10px;
  text-decoration: none;
  white-space: nowrap;
  box-shadow: 2px 2px 0 rgba(42, 38, 32, 0.2);
}
.stub-btn:active { transform: translate(1px, 1px); box-shadow: 1px 1px 0 rgba(42, 38, 32, 0.2); }

/* 骨架：纸色 shimmer */
.tp-skeleton {
  border-radius: 8px;
  background: linear-gradient(90deg, var(--tp-paper2) 25%, #f7f2e6 50%, var(--tp-paper2) 75%);
  background-size: 200% 100%;
  animation: tp-shimmer 1.4s infinite linear;
  min-height: 14px;
}
@keyframes tp-shimmer {
  from { background-position: 200% 0; }
  to { background-position: -200% 0; }
}
.sk-stat-label { display: block; width: 4rem; height: 14px; }
.sk-stat-value { display: block; width: 5.5rem; height: 34px; margin: 0.25rem 0; }
.sk-stat-desc { display: block; width: 80%; height: 13px; }
.sk-stub-stamp { display: block; width: 7rem; height: 26px; flex-shrink: 0; }
.sk-stub-watch { display: block; flex: 1; height: 13px; }
.sk-stub-btn { display: block; width: 5rem; height: 36px; flex-shrink: 0; }

@media (prefers-reduced-motion: reduce) {
  .tp-skeleton { animation-duration: 0.001s !important; }
  .stub-admit { transform: none; }
}

/* 375px 窄屏：数字收紧、徽标隐藏，保证两行一行内完整显示 */
@media (max-width: 640px) {
  .ticket-stat { padding: 12px 14px 10px; }
  .stat-label svg { display: none; }
  .stat-value { font-size: 23px; }
  .stat-badge { display: none; }
  .stub-admit { font-size: 10px; letter-spacing: 0.12em; }
  .stub-plan { max-width: 3.5rem; }
  .stub-watch { font-size: 10px; }
  .stub-btn { min-width: 4.25rem; min-height: 34px; padding: 0 1rem; }
}
</style>
