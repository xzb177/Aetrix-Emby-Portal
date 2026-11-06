<template>
  <RouterLink v-if="to" :to="to" class="pc-row">
    <span class="pc-row-ic" aria-hidden="true"><component :is="icon" :size="17" /></span>
    <span class="pc-row-name">{{ name }}</span>
    <span v-if="meta" class="pc-row-meta">{{ meta }}</span>
    <ChevronRight :size="15" class="pc-chev" aria-hidden="true" />
  </RouterLink>
  <button v-else type="button" class="pc-row" :class="{ danger }" @click="emit('click')">
    <span class="pc-row-ic" aria-hidden="true"><component :is="icon" :size="17" /></span>
    <span class="pc-row-name">{{ name }}</span>
    <span v-if="meta" class="pc-row-meta">{{ meta }}</span>
    <ChevronDown
      v-if="expandable"
      :size="15"
      class="pc-chev pc-chev-down"
      :class="{ open: expanded }"
      aria-hidden="true"
    />
    <ChevronRight v-else :size="15" class="pc-chev" aria-hidden="true" />
  </button>
</template>

<script setup lang="ts">
/**
 * 个人中心 / 账号设置共用的入口行（照已批准效果图的 .row 口径）：
 * 图标盒（墨边纸底）+ 名称 + 右侧状态小字 + 箭头。
 * to 有值渲染 RouterLink（整行跳转），否则渲染 button（点击/展开由父级处理）。
 * meta 只传现有接口拿得到的真实摘要，拿不到就别传（不编造）。
 */
import type { Component } from 'vue'
import { RouterLink } from 'vue-router'
import { ChevronRight, ChevronDown } from 'lucide-vue-next'

defineProps<{
  icon: Component
  name: string
  meta?: string
  to?: string | { path: string; hash?: string }
  danger?: boolean
  expandable?: boolean
  expanded?: boolean
}>()

const emit = defineEmits<{ (e: 'click'): void }>()
</script>

<style scoped>
.pc-row {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 100%;
  padding: 12.5px 14px;
  background: none;
  border: none;
  cursor: pointer;
  color: var(--ink);
  font: inherit;
  text-align: left;
  text-decoration: none;
  box-sizing: border-box;
}
.pc-row:active { background: var(--paper2); }
.pc-row-ic {
  width: 31px;
  height: 31px;
  flex: none;
  border: 1.5px solid var(--ink);
  border-radius: 8px;
  background: var(--paper);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--ink);
}
.pc-row-name { font-size: 14.5px; font-weight: 600; white-space: nowrap; }
.pc-row-meta {
  margin-left: auto;
  font-size: 12.5px;
  color: var(--ink2);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  font-variant-numeric: tabular-nums;
}
.pc-chev { margin-left: auto; color: var(--ink2); flex: none; }
.pc-row:has(.pc-row-meta) .pc-chev { margin-left: 0; }
.pc-chev-down { transition: transform 0.15s ease; }
.pc-chev-down.open { transform: rotate(180deg); }
.pc-row.danger .pc-row-name { color: var(--red); }
.pc-row.danger .pc-row-ic { color: var(--red); }
</style>
