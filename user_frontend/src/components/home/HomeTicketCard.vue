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
  <!-- v4 纸片风电影票：概念保留（打孔虚线 + 票根），视觉按纸色设计系统重做，
       不再使用暗房深色 --au-* 变量 -->
  <section v-if="loading" class="ticket" aria-hidden="true">
    <div class="ticket-main">
      <div v-for="i in 3" :key="i" class="ticket-stat">
        <span class="tp-skeleton sk-stat-label"></span>
        <span class="tp-skeleton sk-stat-value"></span>
        <span class="tp-skeleton sk-stat-desc"></span>
      </div>
    </div>
    <div class="ticket-stub">
      <span class="tp-skeleton sk-stub-btn"></span>
    </div>
  </section>
  <section v-else class="ticket" aria-label="我的资产">
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
        <!-- 数字下面固定一行：订阅是细进度条，其余是一行 ≤6 字的状态。
             三格这一行等高，大数字因此落在同一条基线上 -->
        <span class="stat-foot">
          <span
            v-if="c.progress !== null"
            class="stat-progress"
            :title="`套餐周期已过 ${c.progress}%`"
          >
            <!-- 进度条用 scaleX 而不是改 width：合成器线程就能跑，不触发布局 -->
            <span class="stat-progress-fill" :style="{ transform: `scaleX(${c.progress / 100})` }"></span>
          </span>
          <span v-else-if="c.short" class="stat-short">{{ c.short }}</span>
        </span>
        <span class="stat-desc">{{ c.desc }}</span>
        <span class="stat-note">{{ c.note }}</span>
      </component>
    </div>
    <!-- 票根：打孔虚线 + 上下半圆缺口；ADMIT ONE 做成红色印章；按钮是墨色纸片钮 -->
    <div class="ticket-stub">
      <span class="stub-admit" aria-hidden="true">
        ADMIT ONE<span v-if="planName" class="stub-plan"><i> · </i>{{ planName }}</span>
      </span>
      <RouterLink to="/store" class="stub-btn">
        {{ memberFooter }}
      </RouterLink>
    </div>
  </section>
</template>

<style scoped>
/* v4 纸片风电影票：token 就地定义（首页不在 .v4-scope 内，用不 v4-paper.css 的变量） */
.ticket {
  --tp-paper: #FFFDF6;
  --tp-paper2: #EFE7D6;
  --tp-ink: #2A2620;
  --tp-ink2: #6B6257;
  --tp-line: #D8CDB8;
  --tp-gold: #C99B3F;
  --tp-red: #D9482B;
  --tp-green: #4A7C59;

  display: grid;
  grid-template-columns: 1fr auto;
  background: var(--tp-paper);
  border: 1.5px solid var(--tp-ink);
  border-radius: 12px;
  box-shadow: 3px 3px 0 rgba(42, 38, 32, 0.12);
  overflow: hidden;
  color: var(--tp-ink);
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
.stat-value { font-size: 28px; font-weight: 800; line-height: 1.1; color: var(--tp-ink); }
.stat-unit { font-size: 13px; color: var(--tp-ink2); }
.stat-foot { height: 18px; display: flex; align-items: center; }
.stat-progress {
  flex: 1;
  height: 6px;
  background: var(--tp-paper2);
  border: 1px solid var(--tp-line);
  border-radius: 3px;
  overflow: hidden;
}
.stat-progress-fill {
  display: block;
  height: 100%;
  width: 100%;
  background: var(--tp-gold);
  transform-origin: left;
}
.stat-short { font-size: 12px; font-weight: 700; color: var(--tp-ink2); }
.stat-foot .stat-short { display: none; }
.stat-desc, .stat-note { font-size: 12px; color: var(--tp-ink2); line-height: 1.5; }

/* 票根：左侧一条打孔虚线（全卡唯一一处虚线），上下各一个半圆缺口。
   缺口圆用页面底色填充——首页浅色底为 #f5f1ea，深色模式跟随 --au-bg */
.ticket-stub {
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 14px 18px;
  border-left: 2px dashed var(--tp-line);
  min-width: 128px;
  background: var(--tp-paper2);
}
.ticket-stub::before,
.ticket-stub::after {
  content: '';
  position: absolute;
  left: -12px;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: var(--au-bg, #f5f1ea);
  border: 1.5px solid var(--tp-ink);
}
.ticket-stub::before {
  top: -13px;
  clip-path: inset(50% 0 0 0);
}
.ticket-stub::after {
  bottom: -13px;
  clip-path: inset(0 0 50% 0);
}
/* ADMIT ONE 做成红色印章：描边 + 微转 + 宽松字距 */
.stub-admit {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  min-width: 0;
  max-width: 100%;
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0.22em;
  color: var(--tp-red);
  border: 1.5px solid var(--tp-red);
  border-radius: 4px;
  padding: 5px 10px 5px 12px;
  transform: rotate(-3deg);
  white-space: nowrap;
  background: rgba(217, 72, 43, 0.05);
}
/* 宽屏票根是竖条：ADMIT ONE 一行、套餐名另起一行；窄屏横排时才用「 · 」连成一行 */
.stub-plan {
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 100%;
  letter-spacing: 0.1em;
  font-size: 10px;
  color: var(--tp-ink2);
}
.stub-plan i { display: none; font-style: normal; }
/* 票根按钮：v4 墨色纸片钮 */
.stub-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 6rem;
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
.sk-stat-value { display: block; width: 5.5rem; height: 36px; margin: 0.25rem 0; }
.sk-stat-desc { display: block; width: 80%; height: 13px; }
.sk-stub-btn { display: block; width: 6rem; height: 36px; }

@media (prefers-reduced-motion: reduce) {
  .tp-skeleton { animation-duration: 0.001s !important; }
  .stub-admit { transform: none; }
}

/* 窄屏：整票变竖排，打孔线横过来，缺口挪到左右两侧 */
@media (max-width: 768px) {
  .ticket { grid-template-columns: 1fr; }
  .ticket-main { grid-template-columns: 1fr; }
  .ticket-stat + .ticket-stat {
    border-left: none;
    border-top: 1px dashed var(--tp-line);
  }
  .ticket-stub {
    border-left: none;
    border-top: 2px dashed var(--tp-line);
    flex-direction: row;
    justify-content: space-between;
    background: var(--tp-paper);
    padding: 12px 16px;
  }
  .ticket-stub::before,
  .ticket-stub::after {
    top: -12px;
    bottom: auto;
  }
  .ticket-stub::before {
    left: -13px;
    clip-path: inset(0 0 0 50%);
  }
  .ticket-stub::after {
    left: auto;
    right: -13px;
    clip-path: inset(0 50% 0 0);
  }
  .stub-admit {
    flex-direction: row;
    align-items: baseline;
    gap: 0;
    overflow: hidden;
    transform: rotate(-2deg);
  }
  .stub-plan i { display: inline; }
  .stub-btn { flex-shrink: 0; min-width: 5rem; }
}
/* 640px：移动端只留 ≤6 字短状态，长说明/备注/徽标隐藏，图标数字收紧 */
@media (max-width: 640px) {
  .ticket-stat {
    gap: 0.3125rem;
    padding: 1rem 0.875rem 0.9375rem;
  }
  .stat-label svg { display: none; }
  .stat-value { font-size: 1.75rem; }
  .stat-badge,
  .stat-desc,
  .stat-note { display: none; }
  .stat-foot .stat-short { display: block; }
}
</style>
