<template>
  <div class="v4-passport" role="list" aria-label="口味护照">
    <h3>TASTE PASSPORT · 已集 {{ stampedCount }}/{{ tastes.length }}</h3>
    <div class="v4-stamps">
      <div
        v-for="t in tastes"
        :key="t.key"
        class="v4-stampbox"
        :class="{ got: t.stamped }"
        role="listitem"
        :aria-label="`${t.name}${t.stamped ? '，已集' : '，未集'}`"
      >
        <span class="em">{{ t.emoji }}</span>
        <span class="nm">{{ t.name }}</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { TasteStamp } from '@/composables/usePanelApi'

/** 12 口味固定顺序 + emoji（后端 TASTE_MAP 顺序，不许打乱） */
const TASTE_ORDER: Array<{ key: string; emoji: string }> = [
  { key: 'taste_anime', emoji: '🐉' },
  { key: 'taste_drama_cn', emoji: '📺' },
  { key: 'taste_drama_us', emoji: '🎞️' },
  { key: 'taste_drama_jpkr', emoji: '🌸' },
  { key: 'taste_movie_cn', emoji: '🎬' },
  { key: 'taste_movie_foreign', emoji: '🌍' },
  { key: 'taste_movie_anime', emoji: '✨' },
  { key: 'taste_doc', emoji: '🌊' },
  { key: 'taste_variety', emoji: '🎤' },
  { key: 'taste_kids', emoji: '🧒' },
  { key: 'taste_suspense', emoji: '🔍' },
  { key: 'taste_scifi', emoji: '🚀' },
]

const props = defineProps<{
  /** 后端 tastes：{taste_key: {name, stamped, count}} */
  raw: Record<string, TasteStamp> | undefined
}>()

const tastes = computed(() =>
  TASTE_ORDER.map((o) => {
    const s = props.raw?.[o.key]
    return {
      key: o.key,
      emoji: o.emoji,
      name: s?.name || '未知',
      stamped: !!s?.stamped,
    }
  }),
)

const stampedCount = computed(() => tastes.value.filter((t) => t.stamped).length)
</script>

<style scoped>
/* 取自设计稿 .passport / .stamps / .stampbox */
.v4-passport {
  background: var(--blue);
  border-radius: 14px;
  padding: 16px;
  color: #eaf0fa;
  position: relative;
  overflow: hidden;
}
.v4-passport::after {
  content: "TASTE PASSPORT";
  position: absolute;
  bottom: 6px;
  right: 10px;
  font-size: 11px;
  letter-spacing: 3px;
  opacity: 0.4;
}
.v4-passport h3 {
  font-size: 13px;
  letter-spacing: 2px;
  margin: 0 0 12px;
}
.v4-stamps {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
}
.v4-stampbox {
  aspect-ratio: 1;
  border: 1.5px dashed rgba(255, 255, 255, 0.45);
  border-radius: 10px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  gap: 4px;
  color: rgba(255, 255, 255, 0.55);
}
.v4-stampbox .em {
  font-size: 20px;
  filter: grayscale(1);
  opacity: 0.5;
}
.v4-stampbox .nm {
  font-size: 11px;
}
.v4-stampbox.got {
  border-style: solid;
  border-color: #ffd98a;
  background: rgba(255, 217, 138, 0.12);
  color: #ffe9b8;
}
.v4-stampbox.got .em {
  filter: none;
  opacity: 1;
}
.v4-stampbox.got::after {
  content: "✓ 已集";
  font-size: 9px;
  color: #9be08a;
}
</style>
