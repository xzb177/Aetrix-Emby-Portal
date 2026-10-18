<template>
  <div class="sheet-wall">
          <p class="w-sheet-empty" v-if="wallLoading && wallPosts.length === 0">正在翻墙…</p>
          <ul class="w-wall-list" v-else-if="wallPosts.length > 0">
            <li v-for="p in wallPosts" :key="String(p.id)" class="w-wall-row">
              <span class="w-wall-nick">{{ String(p.nickname ?? '') }}</span>
              <span class="w-wall-time">{{ fmtWallTime(p.created_at) }}</span>
              <p class="w-wall-text">{{ String(p.content ?? '') }}</p>
            </li>
          </ul>
          <p class="w-sheet-empty" v-else>墙上还空着，来留下第一句话吧 💬</p>
          <div class="w-wall-form">
            <input :value="modelValue"
            @input="$emit('update:modelValue', ($event.target as HTMLInputElement).value)" maxlength="60" placeholder="到此一游，留句话吧（60字内）" @keyup.enter="$emit('submit')" aria-label="留言内容" />
            <button type="button" class="w-wall-btn" :disabled="wallPosting || !modelValue.trim()" @click="$emit('submit')">
              {{ wallPosting ? '贴上…' : '贴上墙' }}
            </button>
          </div>
          <p class="w-sheet-foot">每人每 10 分钟可留 1 条</p>
          </div>
</template>

<script setup lang="ts">
/** 到此一游留言墙弹层：输入经 v-model 回到 index（submitWall 逻辑仍在 index） */
defineProps<{
  wallPosts: Array<Record<string, unknown>>
  wallLoading: boolean
  wallPosting: boolean
  modelValue: string
  fmtWallTime: (v: unknown) => string
}>()
defineEmits<{ 'update:modelValue': [v: string]; submit: [] }>()
</script>

<style scoped>
.w-sheet-empty { font-size: 13.5px; color: var(--paper-ink-soft); padding: 12px 0; }
.w-sheet-foot { margin-top: 12px; font-size: 11px; color: var(--paper-ink-soft); text-align: center; letter-spacing: .08em; }

.w-wall-list {
  list-style: none;
  margin: 0 0 12px;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 40dvh;
  overflow-y: auto;
}
.w-wall-row {
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 8px 12px;
  background: var(--paper-card);
}
.w-wall-nick {
  font-size: 13px;
  font-weight: 600;
  color: var(--paper-teal);
}
.w-wall-time {
  font-size: 11px;
  color: var(--ink2);
  margin-left: 8px;
}
.w-wall-text {
  margin: 4px 0 0;
  font-size: 14px;
  line-height: 1.7;
}
.w-wall-form {
  display: flex;
  gap: 8px;
  margin-top: 4px;
}
.w-wall-form input {
  flex: 1;
  min-width: 0;
  padding: 9px 12px;
  border-radius: 10px;
  border: 1px solid var(--line);
  background: var(--paper-card-soft);
  font-size: 14px;
  color: var(--ink);
}
.w-wall-btn, .w-fortune-btn {
  padding: 9px 18px;
  border-radius: 10px;
  border: none;
  background: var(--paper-teal);
  color: var(--paper-card-soft);
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  white-space: nowrap;
}
.w-wall-btn:disabled, .w-fortune-btn:disabled {
  opacity: .5;
  cursor: default;
}

@media (prefers-reduced-motion: reduce) {
  .sheet-wall * { animation: none !important; transition: none !important; }
}
</style>
