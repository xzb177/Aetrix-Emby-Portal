<template>
  <div class="sheet-letter">
    <WaxLetter :letter="letterData" />
  </div>
</template>

<script setup lang="ts">
import WaxLetter from '@/components/panel/WaxLetter.vue'

/** 昨日信件弹层 */
defineProps<{ letterData: Record<string, unknown> }>()
</script>

<style scoped>
.w-sheet-empty {
  font-size: 13.5px;
  color: var(--paper-ink-soft);
  padding: 12px 0;
}
.w-sheet-foot {
  margin-top: 12px;
  font-size: 11px;
  color: var(--paper-ink-soft);
  text-align: center;
  letter-spacing: 0.08em;
}
@media (prefers-reduced-motion: reduce) {
  .sheet-letter * { animation: none !important; transition: none !important; }
}
</style>
