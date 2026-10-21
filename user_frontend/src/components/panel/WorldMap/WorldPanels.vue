<template>
  <div class="world-panels">
    <div class="feed">
      <h4><i>●</i> 世界动态 · 服务器在忙什么</h4>
      <ul>
        <li v-if="feedState === 'loading'">正在接收世界动态…</li>
        <li v-else-if="feedState === 'error'">动态没加载出来，点上面的建筑照样能玩</li>
        <template v-else>
          <li v-for="(it, idx) in feedItems" :key="idx"><b v-if="it.time">{{ it.time }}</b> {{ it.text }}</li>
          <li v-if="feedItems.length === 0">暂无动态</li>
        </template>
      </ul>
    </div>

    <div class="quest">
      <h4>📋 今日任务</h4>
      <span class="stamp-done" v-if="questPercent >= 100">今日探险完成 ✓</span>
      <button type="button" class="q" :class="{ done: q.done }" v-for="q in quests" :key="q.key" @click="onQuest(q.key)">
        <span class="box">{{ q.done ? '✓' : '' }}</span>{{ q.label }}<span class="rw" v-if="q.rw">{{ q.rw }}</span>
      </button>
      <div class="bar"><i :style="{ width: questPercent + '%' }"></i></div>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { WorldQuest } from './composables/useWorldQuests'

/**
 * 桌面信息面板：世界动态 + 今日任务。
 * 说明书未点名但业务不得删除，收容于此（模板与文案一字未改）。
 */
defineProps<{
  feedItems: Array<{ time: string; text: string }>
  feedState: 'loading' | 'error' | 'ok'
  quests: WorldQuest[]
  questPercent: number
  onQuest: (key: string) => void
}>()
</script>

<style scoped>
.world-panels {
  position: static;
  display: flex;
  gap: 12px;
  margin-top: 12px;
  pointer-events: auto;
}
.feed {
  position: static;
  flex: 1 1 0;
  min-width: 0;
  overflow-wrap: break-word;
  width: auto;
  background: var(--paper-card-soft);
  border: 1px solid var(--paper-hairline);
  border-radius: 1.11cqw;
  padding: 0.97cqw 1.11cqw;
}

.feed h4 {
  font-size: 0.90cqw;
  letter-spacing: 0.10cqw;
  color: var(--paper-ink-soft);
  margin-bottom: 0.56cqw;
}

.feed h4 i {
  color: #4A7C59;
  font-style: normal;
}

.feed ul {
  margin: 0;
  padding: 0;
}

.feed li {
  list-style: none;
  font-size: 0.90cqw;
  color: var(--paper-ink-soft);
  padding: 0.42cqw 0;
  border-top: 1px dashed var(--paper-hairline);
}

.feed li:first-of-type {
  border-top: none;
}

.feed li b {
  color: var(--paper-ink-deep);
  margin-right: 0.42cqw;
}

.quest {
  position: relative;
  flex: 1 1 0;
  min-width: 0;
  overflow-wrap: break-word;
  width: auto;
  background: var(--paper-card-soft);
  border: 1px solid var(--paper-hairline);
  border-radius: 1.11cqw;
  padding: 0.97cqw 1.11cqw;
}

.quest .stamp-done { position: absolute; top: -11px; right: 12px; transform: rotate(-6deg); border: 2px solid #D9482B; color: #D9482B; background: rgba(255,253,246,.92); border-radius: 6px; padding: 2px 9px; font-size: 11px; font-weight: 600; letter-spacing: 1px; }

.quest h4 {
  font-size: 0.90cqw;
  letter-spacing: 0.10cqw;
  color: var(--paper-ink-soft);
  margin-bottom: 0.42cqw;
}

.quest .q {
  display: flex;
  align-items: center;
  gap: 0.63cqw;
  font-size: 0.94cqw;
  padding: 0.49cqw 0;
  border: none;
  border-top: 1px dashed var(--paper-hairline);
  background: none;
  width: 100%;
  text-align: left;
  font-family: inherit;
  color: inherit;
  cursor: pointer;
}

.quest .q:first-of-type {
  border-top: none;
}

.quest .box {
  width: 1.32cqw;
  height: 1.32cqw;
  border-radius: 0.42cqw;
  border: 1.5px solid #b9ac8f;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 0.83cqw;
  color: #fff;
  flex: 0 0 auto;
}

.quest .q.done {
  color: var(--paper-ink-soft);
}

.quest .q.done .box {
  background: #4A7C59;
  border-color: #4A7C59;
}

.quest .q .rw {
  margin-left: auto;
  font-size: 0.83cqw;
  color: var(--paper-teal);
  font-weight: 600;
}

.quest .bar {
  height: 0.42cqw;
  background: #EFE7D6;
  border-radius: 99px;
  margin-top: 0.69cqw;
  overflow: hidden;
}

.quest .bar i {
  display: block;
  height: 100%;
  background: var(--paper-teal);
  border-radius: 99px;
  transition: width .3s ease;
}
@media (max-width: 1023px) {
  .world-panels {
    flex-direction: column;
  }
  .feed,
  .quest {
    border-radius: 12px;
    padding: 12px 14px;
  }
  .feed h4,
  .quest h4 {
    font-size: 12px;
    letter-spacing: 0.04em;
    margin-bottom: 6px;
  }
  .feed li {
    font-size: 14px;
    padding: 8px 0;
  }
  .quest .q {
    font-size: 14px;
    min-height: 44px;
    padding: 8px 0;
    gap: 8px;
  }
  .quest .box {
    width: 18px;
    height: 18px;
    font-size: 12px;
    border-radius: 6px;
  }
  .quest .q .rw { font-size: 12px; }
  .quest .bar { height: 3px; margin-top: 8px; }
}
</style>
