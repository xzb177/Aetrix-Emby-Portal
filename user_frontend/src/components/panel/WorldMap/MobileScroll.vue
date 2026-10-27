<template>
  <div class="m-scroll" ref="scrollEl">
    <!-- 第一幕 · 天空幕 -->
    <section class="act act-sky">
      <div class="sky-sun" aria-hidden="true"></div>
      <div class="sky-cloud sc1" aria-hidden="true"></div>
      <div class="sky-cloud sc2" aria-hidden="true"></div>
      <div class="sky-cloud sc3" aria-hidden="true"></div>
      <p class="sky-hello">{{ greetingText }}，{{ nickname }}</p>
      <p class="sky-daypart" v-if="daypartLine">{{ daypartLine }}</p>
      <div class="sky-pills">
        <span class="spill" v-if="streak > 0">连签 {{ streak }} 天<template v-if="streakHint"> · {{ streakHint }}</template></span>
        <span class="spill" v-if="memberInfo">Lv.{{ memberInfo.level }} {{ memberInfo.level_name }}</span>
        <span class="spill" v-if="pointsText">{{ pointsText }} 积分</span>
      </div>
      <div class="sky-quest">
        <p class="sky-quest-title">今日探险 {{ questDoneCount }}/{{ quests.length }}<template v-if="questNextLabel"> · 还差：{{ questNextLabel }}</template></p>
        <div class="sky-quest-bar"><i :style="{ width: questPercent + '%' }"></i></div>
        <div class="sky-quest-btns">
          <button type="button" v-for="q in quests" :key="q.key" class="sq" :class="{ done: q.done }" @click="onQuest(q.key)">
            <span class="sq-box">{{ q.done ? '✓' : '' }}</span>{{ q.label }}
          </button>
        </div>
      </div>
    </section>

    <!-- 第二幕 · 平原幕（daily 2 列卡片沿小径排布） -->
    <section class="act act-plain">
      <p class="act-label"><span class="act-en">PLAIN</span>平原幕 · 每日必做</p>
      <div class="plain-trail" aria-hidden="true">
        <span class="footprint f1"></span>
        <span class="footprint f2"></span>
        <span class="footprint f3"></span>
        <span class="footprint f4"></span>
      </div>
      <div class="pin-grid-2">
        <div class="pin-tape" v-for="f in groupOf('daily')" :key="f.key">
          <span class="washi" aria-hidden="true"></span>
          <FacilityPin :facility="f" :sub="subFor(f)" :status="statusFor(f)" :selected="selectedKey === f.key" @select="selectFacility" />
        </div>
      </div>
    </section>

    <!-- 第三幕 · 高地幕（social + asset 横滑卡带） -->
    <section class="act act-high">
      <div class="paper-divider" aria-hidden="true"><span>✦</span></div>
      <p class="act-label"><span class="act-en">HIGHLAND</span>高地幕 · 传讯与账房</p>
      <div class="hstrip">
        <FacilityPin
          v-for="f in [...groupOf('social'), ...groupOf('asset')]"
          :key="f.key"
          class="hcard"
          :facility="f"
          :sub="subFor(f)"
          :status="statusFor(f)"
          :selected="selectedKey === f.key"
          @select="selectFacility"
        />
      </div>
    </section>

    <!-- 第四幕 · 村落幕（style + account + 特殊大锚点） -->
    <section class="act act-village">
      <div class="paper-divider" aria-hidden="true"><span>✦</span></div>
      <p class="act-label"><span class="act-en">VILLAGE</span>村落幕 · 生活与心愿</p>
      <div class="pin-grid-2">
        <FacilityPin
          v-for="f in [...groupOf('style'), ...groupOf('account')]"
          :key="f.key"
          :facility="f"
          :sub="subFor(f)"
          :status="statusFor(f)"
          :selected="selectedKey === f.key"
          @select="selectFacility"
        />
      </div>
      <div class="big-anchors">
        <button
          v-for="a in specialAnchors"
          :key="a.key"
          type="button"
          class="big-anchor"
          :class="{ 'fortune-pending': a.key === 'fortune' && !fortuneDrew }"
          @click="anchorOn[a.handler]()"
          :aria-label="a.name"
        >
          <span class="firelight" aria-hidden="true"></span>
          <AnchorArt :art="a.art" class="big-art" />
          <span class="big-name">{{ a.name }}</span>
          <span class="big-sub">{{ anchorSubs[a.key] }}</span>
        </button>
      </div>
      <p class="act-label sub-label"><span class="act-en">SHORTCUTS</span>快捷传送</p>
      <div class="quick-row">
        <button v-for="q in quickLinks" :key="q.key" type="button" class="qlink" @click="anchorOn[q.handler]()">
          {{ q.name }}<span class="qdot" v-if="q.key === 'parcels' && remaining > 0">{{ remaining }}</span><span class="qdot" v-if="q.key === 'post' && hasLetter && !letterRead">1</span>
        </button>
      </div>
    </section>

    <!-- 底部悬浮胶囊 -->
    <button type="button" class="m-capsule" @click="toTop">
      已探 {{ exploredCount }}/{{ exploredTotal }}<template v-if="exploredNext"> · 还差：{{ exploredNext }}</template><template v-else> · 全部探完 ✓</template>
    </button>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { gameFacilities, type GameFacility } from '@/config/game'
import FacilityPin from './FacilityPin.vue'
import AnchorArt from './AnchorArt.vue'
import type { WorldAnchorSubs, WorldAnchorHandlers } from './WorldCanvas.vue'
import type { WorldQuest } from './composables/useWorldQuests'

/**
 * 手机 C1 长卷轴：天空幕 → 平原幕 → 高地幕 → 村落幕。
 * 设施清单与副标题文案与桌面同源（同一 gameFacilities / 同一 subFor）。
 * 装饰（火光/足迹/纸胶带）只在卷轴版出现。
 */
const props = defineProps<{
  greetingText: string
  nickname: string
  daypartLine: string
  streak: number
  streakHint: string
  pointsText: string | null
  memberInfo: { level: number; level_name: string } | null
  quests: WorldQuest[]
  questPercent: number
  questDoneCount: number
  questNextLabel: string
  onQuest: (key: string) => void
  subFor: (f: GameFacility) => string
  statusFor: (f: GameFacility) => 'todo' | 'done' | undefined
  selectedKey: string | null
  selectFacility: (f: GameFacility) => void
  anchorSubs: WorldAnchorSubs
  anchorOn: WorldAnchorHandlers
  fortuneDrew: boolean
  remaining: number
  hasLetter: boolean
  letterRead: boolean
  exploredCount: number
  exploredTotal: number
  exploredNext: string
}>()

const scrollEl = ref<HTMLElement | null>(null)

function groupOf(group: string): GameFacility[] {
  return gameFacilities
    .filter((f) => f.group === group && f.enabled !== false)
    .sort((a, b) => a.order - b.order)
}

const specialAnchors = computed(() => [
  { key: 'stele', art: 'stele', name: '观影碑', handler: 'stele' },
  { key: 'wall', art: 'wall', name: '留言墙', handler: 'wall' },
  { key: 'fortune', art: 'fortune', name: '抽签亭', handler: 'fortune' },
] as const)

const quickLinks = computed(() => [
  { key: 'parcels', name: '封存驿站', handler: 'parcels' },
  { key: 'post', name: '邮局', handler: 'post' },
  { key: 'board', name: '告示牌', handler: 'board' },
  { key: 'cinema', name: '梦幻影院', handler: 'cinema' },
  { key: 'lighthouse', name: '传送灯塔', handler: 'lighthouse' },
  { key: 'shop', name: '补给站', handler: 'shop' },
  { key: 'collector', name: '收藏家小屋', handler: 'collector' },
  { key: 'gamehall', name: '游戏大厅', handler: 'gamehall' },
] as const)

function toTop() {
  scrollEl.value?.scrollTo({ top: 0, behavior: 'smooth' })
}
</script>

<style scoped>
.m-scroll {
  position: relative;
  height: 100dvh;
  /* 底部垫 --paper-dock-space 避让全局底部导航栏（App.vue 的 PanelTabbar），否则最后一节被压住滑不上来 */
  box-sizing: border-box;
  padding-bottom: var(--paper-dock-space);
  overflow-y: auto;
  -webkit-overflow-scrolling: touch;
  overscroll-behavior-y: contain;
  background: var(--paper-canvas);
  font-family: "PingFang SC", "Microsoft YaHei", system-ui, sans-serif;
  color: var(--paper-ink-deep);
}
.act {
  position: relative;
  padding: 28px 20px;
}
/* 第一幕 · 天空幕 */
.act-sky {
  min-height: 46dvh;
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
  gap: 8px;
  background: linear-gradient(180deg, var(--paper-canvas) 0%, #f4ecdc 100%);
  overflow: hidden;
}
.sky-sun {
  position: absolute;
  top: 34px;
  right: 44px;
  width: 64px;
  height: 64px;
  border-radius: 50%;
  background: var(--paper-card-soft);
  border: 2px dashed var(--paper-hairline);
}
.sky-cloud {
  position: absolute;
  height: 22px;
  border-radius: 999px;
  background: var(--paper-card-soft);
  opacity: 0.9;
}
.sc1 { width: 120px; top: 60px; left: 8%; }
.sc2 { width: 84px; top: 110px; left: 30%; opacity: 0.7; }
.sc3 { width: 100px; top: 44px; left: 55%; opacity: 0.8; }
.sky-hello {
  margin: 0;
  font-size: 16px;
  line-height: 1.5;
  font-weight: 500;
}
.sky-daypart {
  margin: 0;
  font-size: 14px;
  line-height: 1.43;
  color: var(--paper-ink-soft);
}
.sky-pills {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 4px;
}
.spill {
  font-size: 12px;
  line-height: 1.33;
  padding: 6px 12px;
  border: 1px solid var(--paper-hairline);
  border-radius: 999px;
  background: var(--paper-card-soft);
  color: var(--paper-ink-soft);
}
.sky-quest {
  margin-top: 8px;
  padding: 16px;
  background: var(--paper-card-soft);
  border: 1px solid var(--paper-hairline);
  border-radius: 16px;
}
.sky-quest-title {
  margin: 0 0 8px;
  font-size: 14px;
  font-weight: 500;
}
.sky-quest-bar {
  height: 3px;
  border-radius: 99px;
  background: var(--paper-hairline);
  overflow: hidden;
  margin-bottom: 10px;
}
.sky-quest-bar i {
  display: block;
  height: 100%;
  background: var(--paper-teal);
  border-radius: 99px;
  transition: width var(--au-med);
}
.sky-quest-btns {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.sq {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 44px;
  padding: 8px 4px;
  background: none;
  border: none;
  border-top: 1px dashed var(--paper-hairline);
  font-size: 14px;
  color: var(--paper-ink-deep);
  text-align: left;
  cursor: pointer;
  font-family: inherit;
}
.sq-box {
  width: 22px;
  height: 22px;
  flex: none;
  border: 1px solid var(--paper-hairline);
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  color: var(--paper-teal);
}
.sq.done { color: var(--paper-ink-soft); }
.sq.done .sq-box {
  background: var(--paper-teal-soft);
  border-color: var(--paper-teal-border);
}
/* 幕标签 */
.act-label {
  margin: 0 0 16px;
  font-size: 14px;
  font-weight: 500;
  letter-spacing: 0.06em;
}
.act-en {
  display: block;
  font-size: 11px;
  font-weight: 500;
  letter-spacing: 0.22em;
  color: var(--paper-teal);
  margin-bottom: 2px;
}
.sub-label { margin-top: 24px; }
/* 第二幕 · 平原幕 */
.act-plain { background: var(--paper-canvas); }
.plain-trail {
  position: absolute;
  left: 50%;
  top: 70px;
  bottom: 20px;
  width: 0;
  border-left: 2px dashed var(--paper-hairline);
}
.footprint {
  position: absolute;
  left: -7px;
  width: 14px;
  height: 18px;
  opacity: 0.5;
}
.footprint::before,
.footprint::after {
  content: "";
  position: absolute;
  width: 6px;
  height: 8px;
  border-radius: 50%;
  background: var(--paper-ink-soft);
}
.footprint::before { left: 0; top: 0; }
.footprint::after { right: 0; top: 8px; }
.f1 { top: 12%; transform: rotate(-12deg); }
.f2 { top: 34%; transform: rotate(10deg); }
.f3 { top: 58%; transform: rotate(-8deg); }
.f4 { top: 80%; transform: rotate(12deg); }
.pin-grid-2 {
  position: relative;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.pin-tape { position: relative; }
.washi {
  position: absolute;
  top: -8px;
  left: 50%;
  width: 56px;
  height: 18px;
  transform: translateX(-50%) rotate(-4deg);
  background: var(--paper-teal-soft);
  border-left: 1px dashed var(--paper-teal-border);
  border-right: 1px dashed var(--paper-teal-border);
  z-index: 2;
}
/* 第三幕 · 高地幕 */
.act-high { background: #f4ecdc; }
.paper-divider {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: -8px 0 20px;
  color: var(--paper-ink-soft);
  font-size: 11px;
}
.paper-divider::before,
.paper-divider::after {
  content: "";
  flex: 1;
  border-top: 2px dashed var(--paper-hairline);
}
.hstrip {
  display: flex;
  gap: 12px;
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  padding: 4px 2px 12px;
  scroll-snap-type: x mandatory;
}
.hcard {
  flex: 0 0 168px;
  scroll-snap-align: start;
}
/* 第四幕 · 村落幕 */
.act-village { background: var(--paper-canvas); }
.big-anchors {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-top: 16px;
}
.big-anchor {
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  min-height: 44px;
  padding: 16px;
  background: var(--paper-card-soft);
  border: 1px solid var(--paper-hairline);
  border-radius: 16px;
  cursor: pointer;
  font-family: inherit;
  overflow: hidden;
  transition: transform var(--au-med), border-color var(--au-fast);
}
.big-anchor:hover {
  transform: translateY(-2px);
  border-color: var(--paper-teal-border);
}
.big-anchor:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--paper-teal-focus);
}
.big-anchor.fortune-pending .big-art {
  animation: fortunePulseM 2.2s ease-in-out infinite;
}
@keyframes fortunePulseM {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.55; }
}
.big-art { width: 120px; }
.big-name {
  font-size: 16px;
  font-weight: 500;
  color: var(--paper-ink-deep);
}
.big-sub {
  font-size: 12px;
  color: var(--paper-ink-soft);
  text-align: center;
}
.firelight {
  position: absolute;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: radial-gradient(circle, var(--paper-teal-soft) 0%, transparent 70%);
  top: 14px;
  right: 18px;
  animation: fireFlicker 3s ease-in-out infinite;
}
@keyframes fireFlicker {
  0%, 100% { opacity: 0.4; transform: scale(1); }
  50% { opacity: 1; transform: scale(1.6); }
}
.quick-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.qlink {
  position: relative;
  min-height: 44px;
  padding: 10px 14px;
  font-size: 14px;
  font-family: inherit;
  color: var(--paper-ink-deep);
  background: var(--paper-card-soft);
  border: 1px solid var(--paper-hairline);
  border-radius: 999px;
  cursor: pointer;
}
.qdot {
  position: absolute;
  top: -6px;
  right: -6px;
  min-width: 20px;
  height: 20px;
  padding: 0 5px;
  border-radius: 999px;
  background: var(--paper-badge, #d9482b);
  color: #fff;
  font-size: 11px;
  line-height: 20px;
  text-align: center;
}
/* 底部悬浮胶囊 */
.m-capsule {
  position: sticky;
  bottom: 16px;
  display: block;
  margin: 0 auto;
  min-height: 44px;
  padding: 12px 20px;
  font-size: 14px;
  font-family: inherit;
  color: var(--paper-ink-deep);
  background: var(--paper-card-soft);
  border: 1px solid var(--paper-hairline);
  border-radius: 999px;
  cursor: pointer;
  transform: translateY(-8px);
}
@media (prefers-reduced-motion: reduce) {
  .big-anchor,
  .sky-quest-bar i {
    transition: none;
  }
  .big-anchor.fortune-pending .big-art,
  .firelight {
    animation: none;
  }
  .m-capsule { transform: none; }
}
</style>
