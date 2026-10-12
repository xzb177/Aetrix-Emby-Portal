<template>
  <div class="sw-row">
    <div class="t">
      <b>{{ toggle.name }}</b>
      <span :class="{ err: !!errMsg }">{{ errMsg || toggle.desc }}</span>
    </div>
    <button
      type="button"
      class="lever"
      :class="{ on: state, busy }"
      :disabled="!toggle.writable || busy"
      :aria-pressed="state"
      :aria-label="`${toggle.name}：${state ? '开' : '关'}${toggle.writable ? '' : '（仅管理员可操作）'}`"
      @click="flip"
    ></button>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { usePanelStore } from '@/stores/panel'
import type { ServerToggle } from '@/composables/usePanelApi'

const props = defineProps<{ toggle: ServerToggle }>()
const panel = usePanelStore()

const state = ref(props.toggle.on)
const busy = ref(false)
const errMsg = ref('')

watch(
  () => props.toggle.on,
  (v) => {
    state.value = v
  }
)

async function flip() {
  if (!props.toggle.writable || busy.value) return
  const next = !state.value
  const prev = state.value
  state.value = next
  busy.value = true
  errMsg.value = ''
  try {
    await panel.setServerToggle(props.toggle.key, next)
  } catch (e) {
    // 失败回滚
    state.value = prev
    errMsg.value = e instanceof Error ? e.message : '操作失败'
  } finally {
    busy.value = false
  }
}
</script>

<style scoped>
/* 取自设计稿 .sw-row / .lever */
.sw-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 16px;
  border-bottom: 1px dotted var(--line);
  gap: 12px;
}
.sw-row:last-child {
  border-bottom: none;
}
.sw-row .t b {
  font-size: 13px;
  display: block;
}
.sw-row .t span {
  font-size: 11px;
  color: var(--ink2);
}
.sw-row .t span.err {
  color: var(--red);
}
.lever {
  width: 56px;
  height: 30px;
  border-radius: 16px;
  background: var(--paper2);
  border: 1.5px solid var(--ink);
  position: relative;
  cursor: pointer;
  transition: background 0.25s;
  flex-shrink: 0;
  padding: 0;
}
.lever::after {
  content: "";
  position: absolute;
  top: 2px;
  left: 3px;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: var(--ink);
  transition: left 0.25s, background 0.25s;
}
.lever.on {
  background: #cde6c5;
}
.lever.on::after {
  left: 27px;
  background: var(--green);
}
.lever:disabled {
  cursor: not-allowed;
  opacity: 0.45;
}
.lever.busy {
  opacity: 0.6;
}
</style>
