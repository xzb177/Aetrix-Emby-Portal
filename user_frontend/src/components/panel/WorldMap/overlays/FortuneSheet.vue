<template>
  <div class="sheet-fortune">
          <div v-if="fortuneLoading && !fortune" class="w-sheet-empty">正在摇签筒…</div>
          <div v-else-if="fortuneDrew" class="w-fortune">
            <div class="w-fortune-level">{{ String(fortune?.level ?? '') }}</div>
            <p class="w-fortune-text">{{ String(fortune?.text ?? '') }}</p>
            <p class="w-fortune-yiji"><b>宜</b> {{ String(fortune?.yi ?? '') }}　<b>忌</b> {{ String(fortune?.ji ?? '') }}</p>
            <p class="w-sheet-foot">每天 1 次 · 0 点刷新</p>
          </div>
          <div v-else class="w-fortune-draw">
            <div class="w-fortune-pot">🎋</div>
            <p class="w-sheet-empty">心诚则灵，点一下抽今日运势</p>
            <button type="button" class="w-fortune-btn" :disabled="fortuneDrawing" @click="$emit('draw')">
              {{ fortuneDrawing ? '摇签中…' : '抽一签' }}
            </button>
          </div>
          </div>
</template>

<script setup lang="ts">
/** 每日抽签弹层：抽签动作 emit 回 index（drawFortune 逻辑仍在 index） */
defineProps<{
  fortune: Record<string, unknown> | null
  fortuneLoading: boolean
  fortuneDrew: boolean
  fortuneDrawing: boolean
}>()
defineEmits<{ draw: [] }>()
</script>

<style scoped>
.w-sheet-empty { font-size: 13.5px; color: var(--paper-ink-soft); padding: 12px 0; }
.w-sheet-foot { margin-top: 12px; font-size: 11px; color: var(--paper-ink-soft); text-align: center; letter-spacing: .08em; }

.w-fortune {
  text-align: center;
  padding: 10px 0 4px;
}
.w-fortune-level {
  display: inline-block;
  font-size: 22px;
  font-weight: 900;
  color: #D9482B;
  border: 2px solid #D9482B;
  border-radius: 12px;
  padding: 6px 22px;
  margin-bottom: 12px;
  transform: rotate(-3deg);
}
.w-fortune-text {
  font-size: 16px;
  line-height: 1.9;
  margin: 0 0 10px;
}
.w-fortune-yiji {
  font-size: 14px;
  color: var(--ink2);
}
.w-fortune-yiji b {
  color: var(--green);
}
.w-fortune-draw {
  text-align: center;
  padding: 16px 0 6px;
}
.w-fortune-pot {
  font-size: 52px;
  margin-bottom: 6px;
}

@media (prefers-reduced-motion: reduce) {
  .sheet-fortune * { animation: none !important; transition: none !important; }
}
</style>
