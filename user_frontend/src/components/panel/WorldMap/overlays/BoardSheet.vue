<template>
  <div class="sheet-board">
          <div v-if="boardData.visible === true" class="w-board">
            <p class="w-board-text">{{ String(boardData.text ?? '') }}</p>
            <p class="w-board-sig">—— {{ String(boardData.signature ?? '服主') }}</p>
          </div>
          <p class="w-sheet-empty" v-else>服主还没留言，改天再来看看</p>
          </div>
</template>

<script setup lang="ts">
/** 服主告示牌弹层 */
defineProps<{ boardData: Record<string, unknown> }>()
</script>

<style scoped>
.w-sheet-empty { font-size: 13.5px; color: var(--paper-ink-soft); padding: 12px 0; }
.w-sheet-foot { margin-top: 12px; font-size: 11px; color: var(--paper-ink-soft); text-align: center; letter-spacing: .08em; }

.w-board-text {
  font-size: 15px;
  line-height: 1.9;
  white-space: pre-wrap;
  margin: 4px 0 10px;
}
.w-board-sig {
  text-align: right;
  font-size: 13px;
  color: var(--ink2);
}

@media (prefers-reduced-motion: reduce) {
  .sheet-board * { animation: none !important; transition: none !important; }
}
</style>
