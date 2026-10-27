<script setup lang="ts">
/**
 * 页头（暗房影院）：琥珀眉题 + 衬线大标题 + 一行说明 + 右侧操作区。
 *
 * 顶栏已经显示了路由标题（meta.title），所以页面里的 PageHeader 通常用来写
 * **这一页在干什么**（description）和放主操作（actions 插槽）；标题可以与顶栏
 * 不同（更口语、更具体）。用法见 ./README.md。
 */
withDefaults(
  defineProps<{
    /** 衬线大标题 */
    title: string
    /** 标题上方的琥珀小眉题（分组名 / 「概览」之类），可省 */
    eyebrow?: string
    /** 标题下一行说明：说这一页做什么、数据口径，别超过两行 */
    description?: string
    /** 标题标签：页面主标题用 h2（顶栏已有 h1），嵌在卡片里用 h3 */
    as?: 'h1' | 'h2' | 'h3'
  }>(),
  { eyebrow: '', description: '', as: 'h2' },
)
</script>

<template>
  <header class="au-page-header">
    <div class="au-page-header__text">
      <p v-if="eyebrow" class="au-page-header__eyebrow">{{ eyebrow }}</p>
      <component :is="as" class="au-page-header__title">
        <slot name="icon" />
        {{ title }}
      </component>
      <p v-if="description || $slots.description" class="au-page-header__desc">
        <slot name="description">{{ description }}</slot>
      </p>
    </div>
    <div v-if="$slots.actions" class="au-page-header__actions">
      <slot name="actions" />
    </div>
  </header>
</template>

<style scoped>
.au-page-header {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
  margin-bottom: 20px;
}

.au-page-header__text { min-width: 0; flex: 1 1 320px; }

.au-page-header__eyebrow {
  margin: 0 0 6px;
  font-size: 11px;
  font-weight: 500;
  letter-spacing: 0.1em;
  color: var(--au-primary);
}

.au-page-header__title {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 0;
  font-family: var(--au-font-serif);
  font-size: 1.5rem;
  font-weight: 700;
  line-height: 1.25;
  letter-spacing: 0.02em;
  color: var(--au-text);
}

.au-page-header__title :deep(svg) { color: var(--au-primary); flex-shrink: 0; }

.au-page-header__desc {
  margin: 6px 0 0;
  max-width: 74ch;
  font-size: 13px;
  line-height: 1.6;
  color: var(--au-text-3);
}

.au-page-header__actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

@media (max-width: 640px) {
  .au-page-header { margin-bottom: 16px; }
  .au-page-header__title { font-size: 1.3125rem; }
}
</style>
