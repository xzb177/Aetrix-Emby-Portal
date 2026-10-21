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
    <!-- 锚点盒不设 transform / z-index。建筑与标签是兄弟层：标签 z-index 更高，才能盖过后面的房子。 -->
    <div class="spot s-cinema" style="left: 28.83%; top: 65.46%;">
      <button type="button" class="mark pin-hit" @click="on.cinema" aria-label="梦幻影院">
        <AnchorArt art="cinema" class="pin art-cinema" />
        <span class="lab-spacer" aria-hidden="true"><span class="name">梦幻影院</span><span class="sub">{{ subs.cinema }}</span></span>
      </button>
      <div class="mark lab-hit" aria-hidden="true" @click="on.cinema">
        <span class="name">梦幻影院</span>
        <span class="sub">{{ subs.cinema }}</span>
      </div>
    </div>
    <div class="spot s-parcels" style="left: 44.64%; top: 68.63%;">
      <button type="button" class="mark pin-hit" @click="on.parcels" aria-label="封存驿站">
        <span class="dot" v-if="remaining > 0">{{ remaining }}</span>
        <AnchorArt art="parcels" class="pin art-parcels" />
        <span class="lab-spacer" aria-hidden="true"><span class="name">封存驿站</span><span class="sub">{{ subs.parcels }}</span></span>
      </button>
      <div class="mark lab-hit" aria-hidden="true" @click="on.parcels">
        <span class="name">封存驿站</span>
        <span class="sub">{{ subs.parcels }}</span>
      </div>
    </div>
    <div class="spot s-lighthouse" style="left: 82.3%; top: 62.5%;">
      <button type="button" class="mark pin-hit lighthouse" @click="on.lighthouse" aria-label="传送灯塔">
        <AnchorArt art="lighthouse" class="pin art-lighthouse" />
        <span class="lab-spacer" aria-hidden="true"><span class="name">传送灯塔</span><span class="sub">{{ subs.lighthouse }}</span></span>
      </button>
      <div class="mark lab-hit" aria-hidden="true" @click="on.lighthouse">
        <span class="name">传送灯塔</span>
        <span class="sub">{{ subs.lighthouse }}</span>
      </div>
    </div>
    <div class="spot s-shop" style="left: 36.10%; top: 83.14%;">
      <button type="button" class="mark pin-hit" @click="on.shop" aria-label="补给站">
        <AnchorArt art="shop" class="pin art-shop" />
        <span class="lab-spacer" aria-hidden="true"><span class="name">补给站</span></span>
      </button>
      <div class="mark lab-hit" aria-hidden="true" @click="on.shop">
        <span class="name">补给站</span>
      </div>
    </div>
    <div class="spot s-collector" style="left: 12.50%; top: 68.63%;">
      <button type="button" class="mark pin-hit" @click="on.collector" aria-label="收藏家小屋">
        <AnchorArt art="collector" class="pin art-collector" />
        <span class="lab-spacer" aria-hidden="true"><span class="name">收藏家小屋</span><span class="sub">{{ subs.collector }}</span></span>
      </button>
      <div class="mark lab-hit" aria-hidden="true" @click="on.collector">
        <span class="name">收藏家小屋</span>
        <span class="sub">{{ subs.collector }}</span>
      </div>
    </div>
    <div class="spot s-post" style="left: 17.35%; top: 47.54%;">
      <button type="button" class="mark pin-hit" @click="on.post" aria-label="邮局">
        <span class="dot" v-if="hasLetter && !letterRead">1</span>
        <AnchorArt art="post" class="pin art-post" />
        <span class="lab-spacer" aria-hidden="true"><span class="name">邮局</span><span class="sub">{{ subs.post }}</span></span>
      </button>
      <div class="mark lab-hit" aria-hidden="true" @click="on.post">
        <span class="name">邮局</span>
        <span class="sub">{{ subs.post }}</span>
      </div>
    </div>
    <div class="spot s-gamehall" style="left: 56.12%; top: 82.24%;">
      <button type="button" class="mark pin-hit" @click="on.gamehall" aria-label="游戏大厅">
        <AnchorArt art="gamehall" class="pin art-gamehall" />
        <span class="lab-spacer" aria-hidden="true"><span class="name">游戏大厅</span></span>
      </button>
      <div class="mark lab-hit" aria-hidden="true" @click="on.gamehall">
        <span class="name">游戏大厅</span>
      </div>
    </div>
    <div class="spot s-stele" style="left: 51.40%; top: 41.42%;">
      <button type="button" class="mark pin-hit" @click="on.stele" aria-label="观影碑">
        <AnchorArt art="stele" class="pin art-stele" />
        <span class="lab-spacer" aria-hidden="true"><span class="name">观影碑</span><span class="sub">{{ subs.stele }}</span></span>
      </button>
      <div class="mark lab-hit" aria-hidden="true" @click="on.stele">
        <span class="name">观影碑</span>
        <span class="sub">{{ subs.stele }}</span>
      </div>
    </div>
    <div class="spot s-board" style="left: 27%; top: 82.5%;">
      <button type="button" class="mark pin-hit" @click="on.board" aria-label="告示牌">
        <AnchorArt art="board" class="pin art-board" />
        <span class="lab-spacer" aria-hidden="true"><span class="name">告示牌</span><span class="sub">{{ subs.board }}</span></span>
      </button>
      <div class="mark lab-hit" aria-hidden="true" @click="on.board">
        <span class="name">告示牌</span>
        <span class="sub">{{ subs.board }}</span>
      </div>
    </div>
    <div class="spot s-wall" style="left: 76.8%; top: 44%;">
      <button type="button" class="mark pin-hit" @click="on.wall" aria-label="留言墙">
        <AnchorArt art="wall" class="pin art-wall" />
        <span class="lab-spacer" aria-hidden="true"><span class="name">留言墙</span><span class="sub">{{ subs.wall }}</span></span>
      </button>
      <div class="mark lab-hit" aria-hidden="true" @click="on.wall">
        <span class="name">留言墙</span>
        <span class="sub">{{ subs.wall }}</span>
      </div>
    </div>
    <div class="spot s-fortune" style="left: 67.09%; top: 66.36%;">
      <button type="button" class="mark pin-hit" :class="{ 'fortune-pending': !fortuneDrew }" @click="on.fortune" aria-label="抽签亭">
        <AnchorArt art="fortune" class="pin art-fortune" />
        <span class="lab-spacer" aria-hidden="true"><span class="name">抽签亭</span><span class="sub">{{ subs.fortune }}</span></span>
      </button>
      <div class="mark lab-hit" aria-hidden="true" @click="on.fortune">
        <span class="name">抽签亭</span>
        <span class="sub">{{ subs.fortune }}</span>
      </div>
    </div>
    <div class="spot s-player" style="left: 49%; top: 88%;">
      <div class="player pin-hit">
        <div class="tok">{{ avatarChar }}</div>
        <span class="lab-spacer" aria-hidden="true"><span class="who">你在这里</span></span>
      </div>
      <div class="lab-hit player-lab" aria-hidden="true">
        <span class="who">你在这里</span>
      </div>
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
/* 锚点不建立层叠上下文。建筑 z-index 较低，标签较高，标签画在所有建筑之上。
   建筑按钮里用不可见占位把房子顶到标签上方，可见标签是兄弟元素，才能盖过别的房子。 */
.spot {
  position: absolute;
  width: 0;
  height: 0;
}
/* 标签在建筑下方仍会压到旁边的房子时，只平移标签，不改建筑尺寸。 */
.pin-hit,
.lab-hit {
  position: absolute;
  left: 0;
  bottom: 0;
  margin: 0;
  padding: 0;
  background: none;
  border: none;
  font: inherit;
  color: inherit;
  text-align: center;
  cursor: pointer;
}
.pin-hit {
  z-index: 6;
  transform: translate(-50%, 0);
  display: flex;
  flex-direction: column;
  align-items: center;
  /* 占位文字不接收点击，避免看不见的标签槽挡住旁边的房子 */
  pointer-events: none;
}
.pin-hit .pin,
.pin-hit .dot {
  pointer-events: auto;
  cursor: pointer;
}
.pin-hit:focus-visible {
  outline: none;
}
.pin-hit:focus-visible .pin {
  outline: 2px solid var(--paper-teal);
  outline-offset: 3px;
  border-radius: 6px;
}
.lab-hit {
  z-index: 8;
  transform: translate(-50%, 0);
  display: flex;
  flex-direction: column;
  align-items: center;
  width: max-content;
  pointer-events: auto;
}
.spot.s-cinema .lab-hit { transform: translate(calc(-50% - 12.3cqw), 5.65cqw); }
.spot.s-parcels .lab-hit { transform: translate(calc(-50% - 0.35cqw), 8.31cqw); }
.spot.s-lighthouse .lab-hit { transform: translate(calc(-50% - 0.35cqw), -0.35cqw); }
.spot.s-shop .lab-hit { transform: translate(calc(-50% + 1.8cqw), -1.2cqw); }
.spot.s-collector .lab-hit { transform: translate(calc(-50% - 0.35cqw), -0.35cqw); }
.spot.s-post .lab-hit { transform: translate(calc(-50% - 0.35cqw), -0.35cqw); }
.spot.s-gamehall .lab-hit { transform: translate(calc(-50% - 0.35cqw), -0.35cqw); }
.spot.s-stele .lab-hit { transform: translate(calc(-50% - 8.15cqw), -14.96cqw); }
.spot.s-board .lab-hit { transform: translate(calc(-50% - 0.35cqw), 1cqw); }
.spot.s-wall .lab-hit { transform: translate(calc(-50% + 14.3cqw), -0.35cqw); }
.spot.s-fortune .lab-hit { transform: translate(calc(-50% + 4.32cqw), -0.35cqw); }
.spot.s-player .lab-hit { transform: translate(calc(-50% - 0.35cqw), -0.35cqw); }
.lab-spacer {
  visibility: hidden;
  display: flex;
  flex-direction: column;
  align-items: center;
  pointer-events: none;
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
.mark .pin {
  flex: 0 0 auto;
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

/* 封存驿站 / 补给站 / 游戏大厅 / 留言墙的类名对不上上面的 cqw 规则，
   上一版因此用了 SVG 固有宽度（1280 下约 300px）。按画布宽锁回这个尺寸，避免 flex 收成标签那么窄。 */
.mark .art-parcels,
.mark .art-shop,
.mark .art-gamehall,
.mark .art-wall { width: 24.94cqw; }

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
  overflow: visible;
}
.mark .sub {
  display: block;
  margin-top: 0.28cqw;
  font-size: 0.83cqw;
  line-height: 1.35;
  color: var(--paper-ink-soft);
  background: var(--paper-card-soft);
  border-radius: 0.42cqw;
  padding: 0.07cqw 0.56cqw;
  white-space: nowrap;
  overflow: visible;
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
.spot:hover .pin {
  transform: translateY(-0.21cqw);
}
.player {
  text-align: center;
  cursor: default;
}
.player.pin-hit {
  z-index: 7;
  cursor: default;
  pointer-events: none;
}
.player-lab {
  z-index: 8;
  cursor: default;
  pointer-events: none;
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
.player .who,
.player-lab .who {
  margin-top: 0.35cqw;
  font-size: 0.83cqw;
  font-weight: 600;
  background: rgba(255, 253, 246, .9);
  border: 1px solid var(--paper-hairline);
  padding: 0.07cqw 0.63cqw;
  border-radius: 99px;
  display: inline-block;
  white-space: nowrap;
}
.pin-hit.fortune-pending .pin { animation: fortunePulse 2.2s ease-in-out infinite; }
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
  .pin-hit.fortune-pending .pin { animation: none; }
  .spot:hover .pin { transform: none; }
  .trail-quest { transition: none; }
}

</style>
