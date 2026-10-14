<template>
  <div class="v4-passport" role="list" aria-label="口味护照">
    <h3>口味护照<template v-if="!rawMissing"> · 已集 {{ stampedCount }}/{{ tastes.length }}</template></h3>
    <div v-if="rawMissing" class="v4-passport-empty">口味数据没拿到，刷新试试</div>
    <div v-else class="v4-stamps">
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

/** 12 口味固定顺序 + emoji + 默认中文名（后端 TASTE_MAP 顺序，不许打乱；
 *  默认名抄后端 panel_achievements.py 的 TASTE_MAP，s.name 拿不到时兜底） */
const TASTE_ORDER: Array<{ key: string; emoji: string; name: string }> = [
  { key: 'taste_anime', emoji: '🐉', name: '国漫' },
  { key: 'taste_drama_cn', emoji: '📺', name: '国产剧' },
  { key: 'taste_drama_us', emoji: '🎞️', name: '欧美剧' },
  { key: 'taste_drama_jpkr', emoji: '🌸', name: '日韩剧' },
  { key: 'taste_movie_cn', emoji: '🎬', name: '华语电影' },
  { key: 'taste_movie_foreign', emoji: '🌍', name: '外语电影' },
  { key: 'taste_movie_anime', emoji: '✨', name: '动画电影' },
  { key: 'taste_doc', emoji: '🌊', name: '纪录片' },
  { key: 'taste_variety', emoji: '🎤', name: '综艺' },
  { key: 'taste_kids', emoji: '🧒', name: '儿童' },
  { key: 'taste_suspense', emoji: '🔍', name: '悬疑' },
  { key: 'taste_scifi', emoji: '🚀', name: '科幻' },
]

const props = defineProps<{
  /** 后端 tastes：{taste_key: {name, stamped, count}} */
  raw: Record<string, TasteStamp> | undefined
}>()

/** 整表没拿到：诚实文案（口径与 CollectorView 一致），不画假章 */
const rawMissing = computed(() => !props.raw)

const tastes = computed(() =>
  TASTE_ORDER.map((o) => {
    const s = props.raw?.[o.key]
    return {
      key: o.key,
      emoji: o.emoji,
      name: s?.name || o.name,
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
  content: "口味护照";
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
/* 整表没拿到时的诚实文案：颜色用文件里已有的白 55% 档，不新增色值 */
.v4-passport-empty {
  font-size: 12px;
  line-height: 1.8;
  color: rgba(255, 255, 255, 0.55);
  padding: 8px 0;
}
.v4-stamps {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
}
.v4-stampbox {
  aspect-ratio: 1;
  border: 1.5px dashed rgba(255, 255, 255, 0.45);
  border-radius: var(--paper-r-md, 10px);
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
  color: var(--pulse-green, #9be08a);
}
</style>
