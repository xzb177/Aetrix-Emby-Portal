<script lang="ts">
/** 放在普通 script 中导出，便于各 view 复用类型和 chip 展示规则。 */
export interface SearchConfig {
  value: string
  placeholder?: string
  mode: 'submit' | 'debounced'
  /** 默认 250ms；传 0 时逐键立即通知，由父组件自行防抖。 */
  debounceMs?: number
}

export interface FilterOption {
  label: string
  value: string | number | boolean
}

export interface FilterConfig {
  key: string
  label: string
  type: 'select' | 'checkbox' | 'custom'
  value: any
  options?: FilterOption[]
  placeholder?: string
  /** 优先使用选项文案，避免 chip 暴露内部枚举值。 */
  chipText?: (value: any, options?: FilterOption[]) => string
  /** 允许父组件定义空值，兼容业务中有效的 false 等取值。 */
  isEmpty?: (value: any) => boolean
}

export interface AppliedFilter {
  key: string
  label: string
  text: string
}

export interface FilterBarProps {
  search?: SearchConfig
  /**
   * 无 search 输入框的页面也想用统一查询按钮时传 true（如 LoginLogs 的双文本框布局）。
   * 查询按钮的渲染条件是 search?.mode === 'submit' || queryButton。
   */
  queryButton?: boolean
  filters?: FilterConfig[]
  /** chips 只展示父组件确认提交的条件，不根据控件草稿自行推导。 */
  appliedFilters?: AppliedFilter[]
  showCount?: boolean
  count?: number
  countLabel?: string
  loading?: boolean
}

export function defaultChipText(value: any, options?: FilterOption[]): string {
  const option = options?.find((item) => item.value === value)
  return option ? option.label : String(value)
}

export function defaultIsEmpty(value: any): boolean {
  return value === '' || value == null || value === false
}
</script>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { ElButton, ElCheckbox, ElInput, ElOption, ElSelect } from 'element-plus'
import { Search, X } from 'lucide-vue-next'

const props = withDefaults(defineProps<FilterBarProps>(), {
  queryButton: false,
  filters: () => [],
  appliedFilters: () => [],
  showCount: false,
  count: 0,
  countLabel: '条',
  loading: false,
})

const emit = defineEmits({
  'update:search': (_value: string) => true,
  'search-submit': () => true,
  'filter-change': (_key: string, _value: FilterConfig['value']) => true,
  query: () => true,
  'remove-chip': (_key: string) => true,
  clear: () => true,
})

// 草稿与已提交值分开，避免 submit 模式下每次输入都改变父组件的查询条件。
const searchDraft = ref(props.search?.value ?? '')
let searchTimer: ReturnType<typeof setTimeout> | undefined
let ignoreNextEmptyInput = false

function cancelSearchTimer(): void {
  if (searchTimer !== undefined) {
    clearTimeout(searchTimer)
    searchTimer = undefined
  }
}

watch(
  () => props.search?.value,
  (value) => {
    // 外部重置优先于尚未发送的草稿，避免旧定时器把重置结果覆盖回去。
    cancelSearchTimer()
    searchDraft.value = value ?? ''
  },
  { flush: 'sync' },
)

watch(
  () => [props.search?.mode, props.search?.debounceMs] as const,
  () => {
    // 配置改变后，旧模式创建的延迟任务不应继续向父组件发送事件。
    cancelSearchTimer()
  },
  { flush: 'sync' },
)

function handleSearchInput(value: string): void {
  searchDraft.value = value

  // Element Plus 清空时还会发出 input，跳过它，避免 debounceMs=0 时重复通知。
  if (ignoreNextEmptyInput && value === '') {
    ignoreNextEmptyInput = false
    return
  }
  ignoreNextEmptyInput = false

  cancelSearchTimer()
  if (props.search?.mode !== 'debounced') return

  const delay = Math.max(0, props.search.debounceMs ?? 250)
  if (delay === 0) {
    emit('update:search', searchDraft.value)
    return
  }

  searchTimer = setTimeout(() => {
    searchTimer = undefined
    emit('update:search', searchDraft.value)
  }, delay)
}

function handleSearchClear(): void {
  cancelSearchTimer()
  searchDraft.value = ''

  ignoreNextEmptyInput = true
  void nextTick(() => {
    // 标记只覆盖本轮清空事件，不能误吞后续用户输入。
    ignoreNextEmptyInput = false
  })

  // 清空代表明确的重置意图，立即通知，避免防抖期间仍展示旧条件的结果。
  emit('update:search', '')
  if (props.search?.mode === 'submit') {
    emit('search-submit')
  }
}

function handleSearchEnter(event: Event | KeyboardEvent): void {
  // 中文输入法确认候选字也可能产生回车，不应因此提前提交。
  // el-input 的 keydown 事件类型是 Event | KeyboardEvent：先窄化再读 isComposing。
  if (event instanceof KeyboardEvent && event.isComposing) return
  if (props.search?.mode !== 'submit') return

  event.preventDefault()
  cancelSearchTimer()
  emit('update:search', searchDraft.value)
  emit('search-submit')
}

function handleQuery(): void {
  cancelSearchTimer()
  // queryButton 借用的页面没有 search 输入框：此时没有草稿可同步，只发 query，
  // 不 emit update:search（父组件没绑这个事件，发了也是空转，但不发更干净）。
  if (props.search) {
    emit('update:search', searchDraft.value)
  }
  emit('query')
}

function handleFilterChange(key: string, value: FilterConfig['value']): void {
  // 控件是受控的，值由父组件回传，组件自身不修改 props。
  emit('filter-change', key, value)
}

function filterChangeHandler(
  key: string,
): (value: FilterConfig['value']) => void {
  // custom 插槽也通过同一事件出口更新，避免各 view 采用不同的数据流。
  return (value: FilterConfig['value']) => handleFilterChange(key, value)
}

onBeforeUnmount(cancelSearchTimer)
</script>

<template>
  <div class="au-filterbar">
    <div class="fb-controls">
      <ElInput
        v-if="props.search"
        v-model="searchDraft"
        class="fb-search"
        clearable
        :placeholder="props.search.placeholder ?? '搜索关键词'"
        :aria-label="props.search.placeholder || '搜索关键词'"
        @input="handleSearchInput"
        @clear="handleSearchClear"
        @keydown.enter="handleSearchEnter"
      >
        <template #prefix>
          <Search class="fb-search-icon" :size="16" aria-hidden="true" />
        </template>
      </ElInput>

      <template v-for="filter in props.filters" :key="filter.key">
        <!--
          clearable：MediaSeek 原来的状态下拉是 clearable 的，点 × 与选「全部状态」等价。
          value-on-clear="" 让清空发 filter-change(key, '') 而不是 undefined，
          与「全部」选项的值口径一致，父组件不用再做 undefined 归一。
        -->
        <ElSelect
          v-if="filter.type === 'select'"
          class="fb-select"
          clearable
          :value-on-clear="''"
          :model-value="filter.value"
          :placeholder="filter.placeholder ?? filter.label"
          :aria-label="filter.label"
          @change="handleFilterChange(filter.key, $event)"
        >
          <ElOption
            v-for="option in filter.options ?? []"
            :key="`${typeof option.value}:${String(option.value)}`"
            :label="option.label"
            :value="option.value"
          />
        </ElSelect>

        <ElCheckbox
          v-else-if="filter.type === 'checkbox'"
          class="fb-checkbox"
          :model-value="filter.value"
          @change="handleFilterChange(filter.key, $event)"
        >
          {{ filter.label }}
        </ElCheckbox>

        <div v-else class="fb-custom">
          <slot
            :name="`filter-${filter.key}`"
            :filter="filter"
            :value="filter.value"
            :on-change="filterChangeHandler(filter.key)"
          />
        </div>
      </template>

      <ElButton
        v-if="props.search?.mode === 'submit' || props.queryButton"
        class="fb-query"
        type="primary"
        native-type="button"
        :loading="props.loading"
        @click="handleQuery"
      >
        查询
      </ElButton>
    </div>

    <!-- chips 只反映已提交条件；移除和清除仅通知父组件，不在这里发起请求。 -->
    <div
      v-if="props.appliedFilters.length > 0 || props.showCount"
      class="fb-chips"
    >
      <TransitionGroup name="fb-chip">
        <span
          v-for="chip in props.appliedFilters"
          :key="chip.key"
          class="fb-chip"
        >
          <span class="fb-chip-text">
            {{ chip.label ? `${chip.label}：${chip.text}` : chip.text }}
          </span>
          <button
            class="fb-chip-remove"
            type="button"
            :aria-label="`移除筛选：${chip.label ? `${chip.label}：${chip.text}` : chip.text}`"
            @click="emit('remove-chip', chip.key)"
          >
            <X :size="14" aria-hidden="true" />
          </button>
        </span>
      </TransitionGroup>

      <button
        v-if="props.appliedFilters.length > 0"
        class="fb-clear"
        type="button"
        @click="emit('clear')"
      >
        清除全部
      </button>

      <span v-if="props.showCount" class="fb-count">
        共 {{ props.count }} {{ props.countLabel }}
      </span>
    </div>
  </div>
</template>

<style scoped>
.au-filterbar {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
  max-width: 100%;
  color: var(--au-text);
}

.fb-controls {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  min-width: 0;
}

.fb-search {
  flex: 1 1 220px;
  min-width: 0;
  max-width: 100%;
  --el-input-bg-color: var(--au-surface-2);
  --el-input-text-color: var(--au-text);
  --el-input-placeholder-color: var(--au-text-4);
  --el-input-border-color: var(--au-border);
  --el-input-hover-border-color: var(--au-text-3);
  --el-input-focus-border-color: var(--au-primary);
}

.fb-search-icon {
  color: var(--au-text-3);
}

.fb-select {
  flex: 0 0 140px;
  width: 140px;
  min-width: 0;
  max-width: 100%;
  --el-fill-color-blank: var(--au-surface-2);
  --el-text-color-regular: var(--au-text);
  --el-text-color-placeholder: var(--au-text-4);
  --el-border-color: var(--au-border);
  --el-border-color-hover: var(--au-text-3);
  --el-color-primary: var(--au-primary);
}

.fb-checkbox {
  flex: 0 0 auto;
  min-width: 0;
  max-width: 100%;
  margin-inline-end: 0;
  white-space: nowrap;
  --el-checkbox-text-color: var(--au-text-2);
  --el-checkbox-checked-text-color: var(--au-primary);
  --el-checkbox-checked-bg-color: var(--au-primary);
  --el-checkbox-checked-input-border-color: var(--au-primary);
  --el-checkbox-input-border-color-hover: var(--au-primary);
}

.fb-checkbox :deep(.el-checkbox__label) {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.fb-custom {
  min-width: 0;
  max-width: 100%;
}

.fb-query {
  flex: 0 0 auto;
  margin: 0;
  --el-button-bg-color: var(--au-primary);
  --el-button-border-color: var(--au-primary);
  --el-button-text-color: var(--au-surface-2);
  --el-button-hover-bg-color: var(--au-primary);
  --el-button-hover-border-color: var(--au-primary);
  --el-button-hover-text-color: var(--au-surface-2);
  --el-button-active-bg-color: var(--au-primary);
  --el-button-active-border-color: var(--au-primary);
  --el-button-active-text-color: var(--au-surface-2);
  --el-button-disabled-bg-color: var(--au-surface-2);
  --el-button-disabled-border-color: var(--au-border);
  --el-button-disabled-text-color: var(--au-text-4);
}

.fb-chips {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  min-width: 0;
}

.fb-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  box-sizing: border-box;
  min-width: 0;
  max-width: 100%;
  padding: 4px 6px 4px 10px;
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-lg);
  background: var(--au-surface-2);
  color: var(--au-text-2);
  font-size: 12.5px;
}

.fb-chip-text {
  min-width: 0;
  overflow-wrap: anywhere;
}

.fb-chip-remove,
.fb-clear {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  box-sizing: border-box;
  min-width: 32px;
  min-height: 32px;
  padding: 6px;
  border: none;
  border-radius: var(--au-r-sm);
  background: none;
  font: inherit;
  cursor: pointer;
  transition: color 120ms ease;
}

.fb-chip-remove {
  color: var(--au-text-3);
}

.fb-chip-remove:hover {
  color: var(--au-danger);
}

.fb-clear {
  color: var(--au-text-2);
  font-size: 12.5px;
}

.fb-clear:hover {
  color: var(--au-primary);
}

.fb-chip-remove:focus-visible,
.fb-clear:focus-visible {
  outline: 2px solid var(--au-primary);
  outline-offset: 2px;
}

.fb-count {
  margin-inline-start: auto;
  color: var(--au-text-3);
  font-size: 12.5px;
}

.fb-chip-enter-active,
.fb-chip-leave-active {
  transition:
    opacity 120ms ease,
    transform 120ms ease;
}

.fb-chip-enter-from,
.fb-chip-leave-to {
  opacity: 0;
  transform: scale(0.96);
}

@media (max-width: 640px) {
  .fb-controls {
    flex-direction: column;
    align-items: stretch;
    flex-wrap: wrap;
  }

  /* 纵向 flex 下移除桌面的 220px basis，避免它变成搜索框高度。 */
  .fb-search,
  .fb-select,
  .fb-custom {
    flex: 0 1 auto;
    width: 100%;
    min-width: 0;
  }

  .fb-checkbox {
    align-self: flex-start;
  }

  /* 小图标不能等同于小触区，手机上保留至少 44px 的可点击范围。 */
  .fb-chip-remove,
  .fb-clear {
    min-width: 44px;
    min-height: 44px;
  }
}

@media (prefers-reduced-motion: reduce) {
  /* deep 同时覆盖 Element Plus 内部节点，避免只关闭自有 chip 的动画。 */
  .au-filterbar,
  .au-filterbar :deep(*) {
    transition: none !important;
    animation: none !important;
  }
}
</style>
