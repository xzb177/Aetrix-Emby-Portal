<template>
  <div class="scene">
    <img class="scene-terrain" :src="terrainUrl" alt="" aria-hidden="true" draggable="false" />
    <svg class="scene-trails" viewBox="0 0 1568 882" aria-hidden="true">
      <path class="trail-main" d="M272,414 Q806,360 1011,374 Q1216,388 1239,463 Q1262,538 1157,559 Q1052,580 966,650 Q880,720 723,724 Q566,728 433,735 Q300,742 248,671 Q196,600 324,586 Q452,572 576,586 T700,600" />
      <path class="trail-quest" :class="{ on: showQuestPath }" d="M272,414 Q806,360 1011,374 Q1216,388 1239,463 Q1262,538 1157,559 Q1052,580 966,650 Q880,720 723,724 Q566,728 433,735 Q300,742 248,671 Q196,600 324,586 Q452,572 576,586 T700,600" />
      <circle class="waypoint" cx="272" cy="414" r="7"/>
    <circle class="waypoint" cx="806" cy="360" r="7"/>
    <circle class="waypoint" cx="1262" cy="538" r="7"/>
    <circle class="waypoint" cx="1052" cy="580" r="7"/>
    <circle class="waypoint" cx="880" cy="720" r="7"/>
    <circle class="waypoint" cx="566" cy="728" r="7"/>
    <circle class="waypoint" cx="300" cy="742" r="7"/>
    <circle class="waypoint" cx="196" cy="600" r="7"/>
    <circle class="waypoint" cx="700" cy="600" r="7"/>
    </svg>
    <button type="button" class="mark m-cinema" style="left: 28.83%; top: 65.46%;" @click="on.cinema" aria-label="梦幻影院">
      <AnchorArt art="cinema" class="pin art-cinema" />
      <span class="name">梦幻影院</span>
      <span class="sub">{{ subs.cinema }}</span>
    </button>
    <button type="button" class="mark m-parcels" style="left: 44.64%; top: 68.63%;" @click="on.parcels" aria-label="封存驿站">
      <span class="dot" v-if="remaining > 0">{{ remaining }}</span>
      <AnchorArt art="parcels" class="pin art-parcels" />
      <span class="name">封存驿站</span>
      <span class="sub">{{ subs.parcels }}</span>
    </button>
    <button type="button" class="mark m-lighthouse lighthouse" style="left: 82.3%; top: 62.5%;" @click="on.lighthouse" aria-label="传送灯塔">
      <AnchorArt art="lighthouse" class="pin art-lighthouse" />
      <span class="name">传送灯塔</span>
      <span class="sub">{{ subs.lighthouse }}</span>
    </button>
    <button type="button" class="mark m-shop" style="left: 36.10%; top: 83.14%;" @click="on.shop" aria-label="补给站">
      <AnchorArt art="shop" class="pin art-shop" />
      <span class="name">补给站</span>
    </button>
    <button type="button" class="mark m-collector" style="left: 12.50%; top: 68.63%;" @click="on.collector" aria-label="收藏家小屋">
      <AnchorArt art="collector" class="pin art-collector" />
      <span class="name">收藏家小屋</span>
      <span class="sub">{{ subs.collector }}</span>
    </button>
    <button type="button" class="mark m-post" style="left: 17.35%; top: 47.54%;" @click="on.post" aria-label="邮局">
      <span class="dot" v-if="hasLetter && !letterRead">1</span>
      <AnchorArt art="post" class="pin art-post" />
      <span class="name">邮局</span>
      <span class="sub">{{ subs.post }}</span>
    </button>
    <button type="button" class="mark m-gamehall" style="left: 56.12%; top: 82.24%;" @click="on.gamehall" aria-label="游戏大厅">
      <AnchorArt art="gamehall" class="pin art-gamehall" />
      <span class="name">游戏大厅</span>
    </button>
    <button type="button" class="mark m-stele" style="left: 51.40%; top: 41.42%;" @click="on.stele" aria-label="观影碑">
      <AnchorArt art="stele" class="pin art-stele" />
      <span class="name">观影碑</span>
      <span class="sub">{{ subs.stele }}</span>
    </button>
    <button type="button" class="mark m-board" style="left: 22%; top: 82.5%;" @click="on.board" aria-label="告示牌">
      <AnchorArt art="board" class="pin art-board" />
      <span class="name">告示牌</span>
      <span class="sub">{{ subs.board }}</span>
    </button>
    <button type="button" class="mark m-wall" style="left: 76.8%; top: 44%;" @click="on.wall" aria-label="留言墙">
      <AnchorArt art="wall" class="pin art-wall" />
      <span class="name">留言墙</span>
      <span class="sub">{{ subs.wall }}</span>
    </button>
    <button type="button" class="mark m-fortune" style="left: 67.09%; top: 66.36%;" :class="{ 'fortune-pending': !fortuneDrew }" @click="on.fortune" aria-label="抽签亭">
      <AnchorArt art="fortune" class="pin art-fortune" />
      <span class="name">抽签亭</span>
      <span class="sub">{{ subs.fortune }}</span>
    </button>
    <div class="player m-player" style="left: 49%; top: 88%;">
      <div class="tok">{{ avatarChar }}</div>
      <span class="who">你在这里</span>
    </div>
    <div class="night-veil" aria-hidden="true"><svg class="veil-moon moon-d" viewBox="-34 -34 68 68" aria-hidden="true"><path d="M15.5 -26.8 A31 31 0 1 0 15.5 26.8 A28 28 0 1 1 15.5 -26.8 Z" fill="#f7f0cf" stroke="#a06c24" stroke-width="1.6" stroke-linejoin="round" transform="rotate(-18)"/></svg><svg class="veil-moon moon-m" viewBox="-34 -34 68 68" aria-hidden="true"><path d="M15.5 -26.8 A31 31 0 1 0 15.5 26.8 A28 28 0 1 1 15.5 -26.8 Z" fill="#f7f0cf" stroke="#a06c24" stroke-width="1.6" stroke-linejoin="round" transform="rotate(-18)"/></svg></div>
  </div>
</template>

<script setup lang="ts">
import AnchorArt from './AnchorArt.vue'
import terrainUrl from '@/assets/world-terrain.svg'

/** 桌面场景层：地形图 + 小径 + 11 建筑锚点 + 玩家 + 夜幕（地标插画逐字搬家，未改一笔） */
export interface WorldAnchorSubs {
  cinema: string
  parcels: string
  lighthouse: string
  collector: string
  post: string
  stele: string
  board: string
  wall: string
  fortune: string
}
export interface WorldAnchorHandlers {
  cinema: () => void
  parcels: () => void
  lighthouse: () => void
  shop: () => void
  collector: () => void
  post: () => void
  gamehall: () => void
  stele: () => void
  board: () => void
  wall: () => void
  fortune: () => void
}

defineProps<{
  subs: WorldAnchorSubs
  on: WorldAnchorHandlers
  /** 未拆包裹数（封存驿站角标） */
  remaining: number
  /** 是否有未读信件（邮局角标） */
  hasLetter: boolean
  letterRead: boolean
  /** 今日已抽签 → 抽签亭停止脉冲 */
  fortuneDrew: boolean
  avatarChar: string
  /** 探险未完成时，小径叠深青虚线 */
  showQuestPath: boolean
}>()
</script>

<style scoped>
.scene {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
}
.mark {
  position: absolute;
  z-index: 20;
  transform: translate(-50%, -100%);
  text-align: center;
  cursor: pointer;
  background: none;
  border: none;
  padding: 0;
  font: inherit;
  color: inherit;
}
.mark .pin {
  margin: 0 auto;
  position: relative;
  transition: transform var(--au-fast);
}
/* 纸痕测绘：地标本体是手绘 SVG（宽按画布 1568 等比，1 单位 = 100/1568 cqw） */
.mark .pin .art-svg {
  display: block;
  width: 100%;
  height: auto;
  overflow: visible;
}
.mark .art-cinema { width: 9.95cqw; }

.mark .art-post { width: 7.65cqw; }

.mark .art-archive { width: 9.18cqw; }

.mark .art-supply { width: 7.14cqw; }

.mark .art-fortune { width: 8.93cqw; }

.mark .art-walltree { width: 9.44cqw; }

.mark .art-board { width: 6.63cqw; }

.mark .art-stele { width: 6.12cqw; }

.mark .art-collector { width: 8.80cqw; }

.mark .art-lighthouse { width: 6.63cqw; }

.mark .art-hall { width: 7.65cqw; }

/* 夜灯层：部件自带 .nl（暖窗光/光晕），只在夜里显形 */
/* 场景昼夜小部件：弯月与萤火虫只在夜里，太阳夜里收起 */
.mark .pin::after {
  content: none;
}
.mark .name {
  margin-top: 0.55cqw;
  display: inline-block;
  background: var(--paper-card-soft);
  color: var(--paper-ink-deep);
  border: 1px solid rgba(39, 37, 30, .10);
  font-size: 0.94cqw;
  font-weight: 600;
  border-radius: 0.56cqw;
  padding: 0.21cqw 0.69cqw;
  letter-spacing: 0.03cqw;
  white-space: nowrap;
}
.mark .sub {
  display: block;
  margin-top: 0.28cqw;
  font-size: 0.83cqw;
  color: var(--paper-ink-soft);
  background: var(--paper-card-soft);
  border-radius: 0.42cqw;
  padding: 0.07cqw 0.56cqw;
}
.mark .dot {
  position: absolute;
  top: -0.30cqw;
  right: calc(50% - 4.60cqw);
  min-width: 1.46cqw;
  height: 1.46cqw;
  border-radius: 99px;
  background: var(--paper-badge, #D9482B);
  color: #fff;
  font-size: 0.83cqw;
  font-weight: 600;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0 0.42cqw;
  border: 2px solid var(--paper-card-soft);
  z-index: 2;
}
.mark:hover .pin {
  transform: translateY(-0.21cqw);
}
.player {
  position: absolute;
  z-index: 25;
  transform: translate(-50%, -100%);
  text-align: center;
}
.player .tok {
  width: 2.78cqw;
  height: 2.78cqw;
  margin: 0 auto;
  border-radius: 50%;
  background: var(--paper-teal);
  border: 0.21cqw solid var(--paper-card-soft);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 600;
  font-size: 1.11cqw;
}
.player .who {
  margin-top: 0.35cqw;
  font-size: 0.83cqw;
  font-weight: 600;
  background: rgba(255, 253, 246, .9);
  border: 1px solid var(--paper-hairline);
  padding: 0.07cqw 0.63cqw;
  border-radius: 99px;
  display: inline-block;
}
.mark.fortune-pending .pin { animation: fortunePulse 2.2s ease-in-out infinite; }
@keyframes fortunePulse { 0%, 100% { opacity: 1; } 50% { opacity: .55; } }



/* 地形与小径层 */
.scene-terrain {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  display: block;
}
.scene-trails {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  z-index: 5;
}
.trail-main {
  fill: none;
  stroke: #c9b98f;
  stroke-width: 5;
  stroke-linecap: round;
  opacity: .45;
}
.trail-quest {
  fill: none;
  stroke: var(--paper-teal);
  stroke-width: 2.6;
  stroke-dasharray: 2 9;
  stroke-linecap: round;
  opacity: 0;
  transition: opacity var(--au-med);
}
.trail-quest.on { opacity: .9; }
.waypoint {
  fill: var(--paper-card-soft);
  stroke: #c9b98f;
  stroke-width: 2.4;
}
@media (prefers-reduced-motion: reduce) {
  .mark .pin { transition: none; }
  .mark.fortune-pending .pin { animation: none; }
  .trail-quest { transition: none; }
}

</style>
