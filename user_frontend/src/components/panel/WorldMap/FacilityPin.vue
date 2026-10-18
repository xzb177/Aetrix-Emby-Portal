<template>
  <button
    type="button"
    class="facility-pin"
    :class="{ 'is-selected': selected, 'is-done': status === 'done' }"
    @click="emit('select', facility)"
    :aria-label="facility.name"
  >
    <span class="pin-dot" v-if="status" :class="`dot-${status}`" aria-hidden="true"></span>
    <span class="pin-head" aria-hidden="true">{{ facility.icon }}</span>
    <span class="pin-stamp">{{ facility.stamp }}</span>
    <span class="pin-name">{{ facility.name }}</span>
    <span class="pin-sub">{{ sub }}</span>
  </button>
</template>

<script setup lang="ts">
import type { GameFacility } from '@/config/game'

/**
 * 设施纸卡：80px 去色头图 + 12px 眉题 + 14px 设施名 + 12px 副标题 + 右上状态点。
 * 眉题与状态点颜色按说明书硬门执行（唯一彩色 #016a71；已完成=灰）。
 * 副标题与桌面/手机同源（guideTip ?? desc，或调用方传入的动态文案）。
 * 点击只 emit('select')，导航与选中态由调用方统一处理。
 */
const props = withDefaults(
  defineProps<{
    facility: GameFacility
    /** 副标题文案（默认 guideTip ?? desc） */
    sub?: string
    /** 状态点：无数据时不渲染 */
    status?: 'todo' | 'done'
    selected?: boolean
  }>(),
  { sub: '', status: undefined, selected: false },
)

const emit = defineEmits<{ select: [f: GameFacility] }>()
const sub = props.sub || props.facility.guideTip || props.facility.desc
</script>

<style scoped>
.facility-pin {
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  width: 100%;
  min-height: 44px;
  padding: 12px 10px;
  background: var(--paper-card-soft, #fdfbfa);
  border: 1px solid var(--paper-hairline, #d1d1cd);
  border-radius: 16px;
  cursor: pointer;
  font-family: "PingFang SC", "Microsoft YaHei", system-ui, sans-serif;
  transition: transform var(--au-med, 0.26s), border-color var(--au-fast, 0.16s);
}
.facility-pin:hover {
  transform: translateY(-2px);
  border-color: var(--paper-teal-soft, rgba(1, 106, 113, 0.35));
}
.facility-pin:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--paper-teal-focus, rgba(1, 106, 113, 0.35));
}
.facility-pin.is-selected {
  box-shadow: inset 0 0 0 1px var(--paper-teal, #016a71);
}
.pin-dot {
  position: absolute;
  top: 10px;
  right: 10px;
  width: 8px;
  height: 8px;
  border-radius: 999px;
}
.pin-dot.dot-todo {
  background: var(--paper-teal, #016a71);
}
.pin-dot.dot-done {
  background: var(--paper-ink-soft, #6d675c);
  opacity: 0.55;
}
.pin-head {
  width: 80px;
  height: 80px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 44px;
  line-height: 1;
  background: var(--paper-canvas, #faf8f5);
  border: 1px solid var(--paper-hairline, #d1d1cd);
  border-radius: 16px;
  filter: sepia(0.5);
}
.pin-stamp {
  margin-top: 6px;
  font-size: 12px;
  line-height: 1.33;
  font-weight: 500;
  color: var(--paper-teal, #016a71);
  letter-spacing: 0.04em;
}
.pin-name {
  font-size: 14px;
  line-height: 1.43;
  font-weight: 500;
  color: var(--paper-ink-deep, #27251e);
}
.pin-sub {
  font-size: 12px;
  line-height: 1.33;
  font-weight: 400;
  color: var(--paper-ink-soft, #6d675c);
  text-align: center;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
@media (prefers-reduced-motion: reduce) {
  .facility-pin {
    transition: none;
  }
  .facility-pin:hover {
    transform: none;
  }
}
</style>
