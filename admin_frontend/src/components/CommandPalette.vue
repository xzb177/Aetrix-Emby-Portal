<script setup lang="ts">
/**
 * 顶栏全局命令搜索（Phase 5）
 *
 * 后台有二十多个页面，侧边栏还分了七组、默认收起两组——**「我要去某个页面」的成本
 * 高于「我要看一眼数据」**。这个面板把三件事收进一个输入框：
 *
 * 1. 全部页面（按侧边栏分组，组名本身就是搜索词的一部分：搜「经营」能出经营那六页）；
 * 2. 常用定位（待处理工单 / 待审求片 / 待支付订单 / 7 天内到期订阅）——与仪表盘 KPI
 *    用**同一套深链**（`/tickets?status=open` 等），落地页的筛选口径不会两边不一样；
 * 3. 后台自身的动作（刷新当前页、切外观、改密码、退出）。
 *
 * **纯前端**：不新增任何接口，也就谈不上鉴权面——命令本身全是既有路由与既有动作。
 * 命令列表由调用方（Layout.vue）传入，导航真源仍然只有侧边栏那一份。
 */
import { computed, nextTick, ref, watch } from 'vue'
import { Search, CornerDownLeft } from 'lucide-vue-next'
import { useFocusTrap } from '@/composables/useFocusTrap'

export interface PaletteItem {
  id: string
  label: string
  /** 分组标题（页面上作为小标题显示） */
  group: string
  /** 右侧灰字：路径或「会做什么」 */
  hint?: string
  /** 额外匹配词（不显示，只参与搜索） */
  keywords?: string
  icon?: unknown
  /** 路由目标；与 run 二选一 */
  to?: string
  run?: () => void
  /** 危险动作（退出登录）：用警示色区分，避免误触 */
  danger?: boolean
}

const props = defineProps<{
  modelValue: boolean
  items: PaletteItem[]
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'run', item: PaletteItem): void
}>()

const query = ref('')
const activeIndex = ref(0)
const panelRef = ref<HTMLElement | null>(null)
const listRef = ref<HTMLElement | null>(null)
const inputRef = ref<HTMLInputElement | null>(null)

const open = computed(() => props.modelValue)
// 自己写的浮层（不是 el-dialog），所以焦点循环要自己关：Tab 不能跑到背后的页面上去
useFocusTrap(panelRef, open)

/** 命中的命令：空输入时保持传入顺序，输入后按「前缀命中优先」排 */
const results = computed(() => {
  const q = query.value.trim().toLowerCase()
  if (!q) return props.items
  const hit = props.items.filter((item) =>
    [item.label, item.group, item.hint, item.keywords]
      .filter(Boolean)
      .some((field) => String(field).toLowerCase().includes(q)),
  )
  // 前缀命中排前面：搜「订单」时「订单」页应该在「待支付订单」前面
  return hit.sort((a, b) => Number(b.label.toLowerCase().startsWith(q)) - Number(a.label.toLowerCase().startsWith(q)))
})

/** 分组后的结果（保持传入顺序，不跨组重排） */
const groups = computed(() => {
  const out: { name: string; items: PaletteItem[] }[] = []
  for (const item of results.value) {
    const last = out[out.length - 1]
    if (last && last.name === item.group) last.items.push(item)
    else out.push({ name: item.group, items: [item] })
  }
  return out
})

/** 键盘上下移动时的扁平索引（跨组连续） */
const flat = computed(() => results.value)

watch(
  () => props.modelValue,
  (val) => {
    if (val) {
      query.value = ''
      activeIndex.value = 0
      // 等面板渲染出来再聚焦，否则 focus() 落在还没挂载的 input 上
      nextTick(() => inputRef.value?.focus())
    }
  },
)

watch(query, () => {
  activeIndex.value = 0
})

/** 键盘移动时把选中项滚进可视区（长列表在手机上很常见） */
watch(activeIndex, () => {
  nextTick(() => {
    listRef.value
      ?.querySelector('.cmd-item.active')
      ?.scrollIntoView({ block: 'nearest' })
  })
})

function close() {
  emit('update:modelValue', false)
}

function move(step: number) {
  const total = flat.value.length
  if (!total) return
  activeIndex.value = (activeIndex.value + step + total) % total
}

function choose(item: PaletteItem | undefined) {
  if (!item) return
  close()
  emit('run', item)
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'ArrowDown') {
    e.preventDefault()
    move(1)
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    move(-1)
  } else if (e.key === 'Enter') {
    e.preventDefault()
    choose(flat.value[activeIndex.value])
  } else if (e.key === 'Escape') {
    e.preventDefault()
    // stopPropagation：顶栏那层也监听 Esc（关侧边栏抽屉），别让一次 Esc 关两个东西
    e.stopPropagation()
    close()
  }
}
</script>

<template>
  <Teleport to="body">
    <div v-if="modelValue" class="cmd-mask" @click.self="close">
      <div
        ref="panelRef"
        class="cmd-panel"
        role="dialog"
        aria-modal="true"
        aria-label="命令搜索"
        @keydown="onKeydown"
      >
        <div class="cmd-head">
          <Search :size="16" class="cmd-head-icon" />
          <input
            ref="inputRef"
            v-model="query"
            class="cmd-input"
            type="text"
            placeholder="搜页面或命令，例如：工单 / 订单 / 缓存 / 退出"
            aria-label="搜索页面或命令"
            autocomplete="off"
            spellcheck="false"
          />
          <kbd class="cmd-kbd">Esc</kbd>
        </div>

        <div v-if="groups.length" ref="listRef" class="cmd-list" role="listbox">
          <div v-for="g in groups" :key="g.name" class="cmd-group">
            <div class="cmd-group-name">{{ g.name }}</div>
            <button
              v-for="item in g.items"
              :key="item.id"
              class="cmd-item"
              :class="{ active: flat[activeIndex]?.id === item.id, danger: item.danger }"
              type="button"
              role="option"
              :aria-selected="flat[activeIndex]?.id === item.id"
              tabindex="-1"
              @click="choose(item)"
              @mouseenter="activeIndex = flat.findIndex((x) => x.id === item.id)"
            >
              <component :is="item.icon" v-if="item.icon" :size="15" class="cmd-item-icon" />
              <span class="cmd-item-label">{{ item.label }}</span>
              <span v-if="item.hint" class="cmd-item-hint">{{ item.hint }}</span>
              <CornerDownLeft v-if="flat[activeIndex]?.id === item.id" :size="13" class="cmd-enter" />
            </button>
          </div>
        </div>

        <div v-else class="cmd-empty">
          没有匹配「{{ query.trim() }}」的命令
          <span>可以试试页面名（工单 / 求片 / 订单）或动作（刷新 / 外观 / 退出）</span>
        </div>

        <div class="cmd-foot">
          <span><kbd class="cmd-kbd">↑</kbd><kbd class="cmd-kbd">↓</kbd> 选择</span>
          <span><kbd class="cmd-kbd">Enter</kbd> 执行</span>
          <span><kbd class="cmd-kbd">Esc</kbd> 关闭</span>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.cmd-mask {
  position: fixed;
  inset: 0;
  z-index: var(--z-modal);
  display: flex;
  justify-content: center;
  align-items: flex-start;
  /* 顶部留白：面板落在顶栏下方，不遮住标题与面包屑 */
  padding: 12vh 16px 16px;
  background: var(--au-scrim);
  backdrop-filter: blur(3px);
}

.cmd-panel {
  width: min(620px, 100%);
  max-height: 70vh;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-lg);
  background: var(--au-surface-2);
  box-shadow: var(--au-shadow-2);
  overflow: hidden;
}

.cmd-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 14px;
  border-bottom: 1px solid var(--au-border);
}

.cmd-head-icon { color: var(--au-text-3); flex-shrink: 0; }

.cmd-input {
  flex: 1;
  min-width: 0;
  border: none;
  outline: none;
  background: transparent;
  color: var(--au-text);
  font-size: var(--font-size-lg);
  font-family: inherit;
}
.cmd-input::placeholder { color: var(--au-text-3); }

.cmd-kbd {
  padding: 1px 6px;
  border: 1px solid var(--au-border);
  border-radius: var(--radius-xs);
  background: var(--au-bg-soft);
  color: var(--au-text-3);
  font-family: var(--font-mono);
  font-size: 11px;
  line-height: 1.6;
}

.cmd-list { overflow-y: auto; padding: 6px; }

.cmd-group-name {
  padding: 8px 10px 4px;
  font-size: 11.5px;
  color: var(--au-text-3);
  letter-spacing: var(--tracking-wide);
}

.cmd-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 9px 10px;
  border: none;
  border-radius: var(--au-r-md);
  background: transparent;
  color: var(--au-text-2);
  font: inherit;
  font-size: var(--font-size-sm);
  text-align: left;
  cursor: pointer;
}

.cmd-item.active {
  background: var(--au-primary-soft);
  color: var(--au-text);
}

.cmd-item.danger .cmd-item-label { color: var(--au-danger); }
.cmd-item.danger.active { background: var(--au-danger-soft); }

.cmd-item-icon { color: var(--au-text-3); flex-shrink: 0; }
.cmd-item.active .cmd-item-icon { color: var(--au-primary); }
.cmd-item-label { flex: 0 1 auto; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cmd-item-hint {
  flex: 1;
  min-width: 0;
  text-align: right;
  font-size: 11.5px;
  color: var(--au-text-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.cmd-enter { color: var(--au-primary); flex-shrink: 0; }

.cmd-empty {
  padding: 28px 16px;
  text-align: center;
  font-size: var(--font-size-sm);
  color: var(--au-text-2);
}
.cmd-empty span {
  display: block;
  margin-top: 6px;
  font-size: var(--font-size-xs);
  color: var(--au-text-3);
}

.cmd-foot {
  display: flex;
  gap: 14px;
  padding: 8px 14px;
  border-top: 1px solid var(--au-border);
  background: var(--au-bg-soft);
  font-size: 11.5px;
  color: var(--au-text-3);
}
.cmd-foot kbd { margin-right: 3px; }

/* 手机：面板贴顶铺满，键盘弹出后仍留得下结果区 */
@media (max-width: 640px) {
  .cmd-mask { padding: 0; align-items: flex-start; }
  .cmd-panel {
    width: 100%;
    max-height: 100dvh;
    border-radius: 0;
    border: none;
  }
  .cmd-input { font-size: var(--font-size-md); }
  .cmd-item-hint { display: none; }
}
</style>
