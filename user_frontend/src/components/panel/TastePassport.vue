<template>
  <section class="v4-passport" aria-labelledby="taste-passport-title">
    <h3 id="taste-passport-title">口味护照<template v-if="!rawMissing"><span aria-live="polite"> · 已集 {{ stampedCount }}/{{ tastes.length }}</span></template></h3>
    <div v-if="loading" class="v4-skeleton" style="height:120px" aria-hidden="true"></div>
    <div v-else-if="error" class="v4-passport-empty" role="alert">{{ error }}<button type="button" class="v4-retry" @click="emit('retry')">重试</button></div>
    <div v-else-if="rawMissing" class="v4-passport-empty" role="status">口味数据没拿到，刷新试试</div>
    <div v-else class="v4-stamps" role="list">
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
  </section>
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
  /** 加载中：显示骨架 */
  loading?: boolean
  /** 加载失败文案：显示错误+重试 */
  error?: string
}>()

/** R48A-10：重试事件，父组件接上后触发重载 */
const emit = defineEmits<{ (e: 'retry'): void }>()

/** 只有 raw 为 null/undefined（整表没拿到）才算缺失；
 *  合法空对象 {} 表示零集章用户，正常渲染空护照（已集 0/12），口径与 CollectorView 一致 */
const rawMissing = computed(() => props.raw == null)

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
  content: "口味护照" / "";
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
  /* R48A-27：底部留白，避开右下角水印 */
  padding-bottom: 18px;
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
  color: rgba(255, 255, 255, 0.75);
}
.v4-stampbox .em {
  font-size: 20px;
  filter: grayscale(1);
  opacity: 0.5;
}
.v4-stampbox .nm {
  font-size: 11px;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.v4-stampbox.got {
  position: relative;
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
  /* R48A-23：替代文本置空，读屏器不再重复朗读（aria-label 已说明） */
  content: "✓ 已集" / "";
  font-size: 12px;
  color: var(--pulse-green, #9be08a);
  position: absolute;
  bottom: 4px;
  left: 0;
  right: 0;
  text-align: center;
}
</style>
