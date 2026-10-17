<script setup lang="ts">
/**
 * 高级设置折叠容器（配置瘦身第 3 批）。
 *
 * 低频调优项的统一收纳：默认收起，展开后才渲染表单项（v-if 懒渲染，
 * 避免初始化一堆隐藏的表单控件）。风格跟随暗房影院设计系统：
 * 实色表面 + 发丝线 + 14px 圆角，只用 --au-* 令牌。
 *
 * - title：折叠头标题，默认「高级设置」
 * - description：头下一行小字说明
 * - count：标题后的灰色计数（如「8 项」）
 * - persistKey：传了就把展开态存 localStorage（默认不持久，每次进页收起）
 * - defaultOpen：默认是否展开（默认 false）
 *
 * 用法：
 *   <AdvancedSettings description="调了也不影响主流程的参数">
 *     <el-form-item label="...">…</el-form-item>
 *   </AdvancedSettings>
 */
import { computed, ref, watch } from 'vue'
import { ChevronDown } from 'lucide-vue-next'

const props = withDefaults(
  defineProps<{
    title?: string
    description?: string
    count?: string
    persistKey?: string
    defaultOpen?: boolean
  }>(),
  { title: '高级设置', description: '', count: '', persistKey: '', defaultOpen: false },
)

const open = ref(false)
if (props.persistKey) {
  try {
    open.value = localStorage.getItem(props.persistKey) === '1' || props.defaultOpen
  } catch {
    open.value = props.defaultOpen
  }
} else {
  open.value = props.defaultOpen
}

watch(open, (v) => {
  if (!props.persistKey) return
  try {
    localStorage.setItem(props.persistKey, v ? '1' : '0')
  } catch {
    /* 忽略存储异常 */
  }
})

const toggle = () => {
  open.value = !open.value
}
const chevronCls = computed(() => ({ 'is-open': open.value }))
</script>

<template>
  <div class="au-advanced" :class="{ 'is-open': open }">
    <button type="button" class="au-advanced__head" :aria-expanded="open" @click="toggle">
      <span class="au-advanced__title">
        <slot name="title">{{ title }}</slot>
        <span v-if="count" class="au-advanced__count">{{ count }}</span>
      </span>
      <ChevronDown :size="15" class="au-advanced__chevron" :class="chevronCls" />
    </button>
    <p v-if="description" class="au-advanced__desc">{{ description }}</p>
    <!-- 懒渲染：收起时不渲染表单项 -->
    <div v-if="open" class="au-advanced__body">
      <slot />
    </div>
  </div>
</template>

<style scoped>
.au-advanced {
  margin-top: 12px;
  background: var(--au-surface-2);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-lg);
}

.au-advanced__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding: 12px 16px;
  background: none;
  border: 0;
  cursor: pointer;
  font-family: var(--au-font-sans);
  color: var(--au-text);
}

.au-advanced__head:focus-visible {
  outline: 2px solid var(--au-border-focus);
  outline-offset: -2px;
  border-radius: var(--au-r-lg);
}

.au-advanced__title {
  font-size: 13.5px;
  font-weight: 600;
  letter-spacing: 0.02em;
  display: flex;
  align-items: center;
  gap: 8px;
}

.au-advanced__count {
  font-size: 12px;
  font-weight: 400;
  color: var(--au-text-4);
}

.au-advanced__chevron {
  color: var(--au-text-3);
  transition: transform var(--au-med) var(--au-ease);
}

.au-advanced__chevron.is-open {
  transform: rotate(180deg);
}

.au-advanced__desc {
  margin: -6px 0 0;
  padding: 0 16px 10px;
  font-size: 12.5px;
  line-height: 1.55;
  color: var(--au-text-3);
}

.au-advanced__body {
  padding: 4px 16px 16px;
  border-top: 1px solid var(--au-border);
  animation: au-advanced-in var(--au-med) var(--au-ease);
}

@keyframes au-advanced-in {
  from { opacity: 0; }
  to { opacity: 1; }
}
</style>
