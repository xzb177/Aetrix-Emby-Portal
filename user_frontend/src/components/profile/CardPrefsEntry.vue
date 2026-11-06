<template>
  <div class="pc-entry">
    <PcEntryRow
      :icon="LayoutGrid"
      name="发现页卡片展示"
      :meta="modeLabel(cardPrefs.mode.value)"
      expandable
      :expanded="open"
      @click="toggle"
    />
    <div v-if="open" class="pc-expand">
      <div class="mode-seg" role="group" aria-label="发现页卡片展示模式">
        <button
          v-for="opt in modeOptions"
          :key="opt.v"
          type="button"
          :class="{ 'is-on': cardPrefs.mode.value === opt.v }"
          :aria-pressed="cardPrefs.mode.value === opt.v"
          @click="cardPrefs.setMode(opt.v)"
        >
          {{ opt.label }}
        </button>
      </div>
      <p class="mode-hint">自动 = 一个服大卡、多个服总览</p>

      <div v-if="serversLoading && !servers.length" class="srv-empty">正在加载…</div>
      <div v-else-if="serversLoadError" class="srv-empty">服务器清单加载失败<button class="v4-retry" @click="() => loadServers()">重试</button></div>
      <div v-else-if="!servers.length" class="srv-empty">还没有可展示的服务器</div>
      <div v-else>
        <div v-for="s in servers" :key="s.id" class="srv-row">
          <div class="srv-info">
            <div class="srv-name">{{ s.name }}</div>
            <div class="srv-tag">{{ serverTagText(s) }}</div>
          </div>
          <button
            type="button"
            class="srv-switch"
            :class="{ 'is-on': cardPrefs.isVisible(s.id) }"
            role="switch"
            :aria-checked="cardPrefs.isVisible(s.id)"
            :aria-label="`${s.name}：在发现页显示`"
            @click="cardPrefs.toggleServer(s.id)"
          ></button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 发现页卡片展示（自旧 AccountSettingsView 原样抽出）：展示模式三段
 * （自动/大卡/总览）＋ 每台服务器显隐开关；偏好走 useServerCardPrefs
 * 单例（与发现页共用同一份状态），服务器清单首次展开才加载。
 */
import { ref } from 'vue'
import { LayoutGrid } from 'lucide-vue-next'
import PcEntryRow from './PcEntryRow.vue'
import { useServerCardPrefs, type ServerCardMode } from '@/composables/useServerCardPrefs'
import { useServerCards, type ServerCardItem } from '@/composables/useServerCards'

const cardPrefs = useServerCardPrefs()
const { servers, load: loadServers, loading: serversLoading, loadError: serversLoadError } = useServerCards()

const open = ref(false)

const modeOptions: { v: ServerCardMode; label: string }[] = [
  { v: 'auto', label: '自动' },
  { v: 'cards', label: '大卡' },
  { v: 'overview', label: '总览' },
]

const ACCESS_LABEL: Record<ServerCardItem['accessMode'], string> = {
  paid: '付费服',
  free: '公益服',
}

function modeLabel(m: ServerCardMode): string {
  return m === 'cards' ? '大卡' : m === 'overview' ? '总览' : '自动'
}
function serverTagText(s: ServerCardItem): string {
  return ACCESS_LABEL[s.accessMode]
}
function toggle() {
  open.value = !open.value
  if (open.value && !serversLoading.value && !servers.value.length) void loadServers()
}
</script>

<style scoped>
.pc-expand {
  border-top: 1px dashed var(--line);
  padding: 12px 14px 14px;
}
/* 发现页卡片展示：模式三段选择 + 每服显隐开关（自旧页原样迁入） */
.mode-seg {
  display: flex;
  border: 1.5px solid var(--paper-ink);
  border-radius: 8px;
  overflow: hidden;
}
.mode-seg button {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 44px;
  background: none;
  border: 0;
  font-size: 12px;
  color: var(--paper-ink);
  cursor: pointer;
}
.mode-seg button + button {
  border-left: 1px solid var(--paper-line);
}
.mode-seg button.is-on {
  background: var(--paper-primary);
  color: #2A2620;
  font-weight: 700;
}
.mode-hint {
  margin: 8px 0 0;
  font-size: 11px;
  color: var(--paper-ink-2);
}
.srv-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 0;
}
.srv-row + .srv-row {
  border-top: 1px dashed var(--paper-line);
}
.srv-info { min-width: 0; }
.srv-name {
  font-size: 14px;
  font-weight: 700;
  color: var(--paper-ink);
}
.srv-tag {
  font-size: 11px;
  color: var(--paper-ink-2);
}
.srv-switch {
  position: relative;
  flex: none;
  width: 48px;
  height: 44px;
  padding: 0;
  border-radius: 999px;
  border: 0;
  background: none;
  cursor: pointer;
}
.srv-switch::before {
  content: '';
  position: absolute;
  left: 6px;
  top: 12px;
  width: 36px;
  height: 20px;
  border-radius: 999px;
  border: 1.5px solid var(--paper-ink);
  background: var(--paper-card);
}
.srv-switch.is-on::before {
  background: var(--paper-primary);
}
.srv-switch::after {
  content: '';
  position: absolute;
  top: 14px;
  left: 8px;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: var(--paper-card);
  border: 1.5px solid var(--paper-ink);
}
.srv-switch.is-on::after {
  left: auto;
  right: 8px;
  background: var(--paper-ink);
}
.srv-empty {
  padding: 10px 0;
  font-size: 12px;
  color: var(--paper-ink-2);
}
</style>
