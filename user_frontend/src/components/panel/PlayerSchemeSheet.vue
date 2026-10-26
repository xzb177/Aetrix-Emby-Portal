<!-- 去看：底部（窄屏）/ 轻菜单（宽屏）选 Rex、SenPlayer、VidHub。
     senplayer 这个键在按钮和提示里显示成 SenPlayer。
     选中后 location.href = scheme，不打开浏览器，也不带具体条目。 -->
<template>
  <Teleport to="body">
    <div v-if="open" class="psp" @keydown.esc.prevent="close">
      <button type="button" class="psp-mask" aria-label="关闭" @click="close"></button>
      <div
        ref="panelEl"
        class="psp-sheet"
        role="dialog"
        aria-modal="true"
        aria-labelledby="psp-title"
        tabindex="-1"
      >
        <div class="psp-head">
          <div>
            <div id="psp-title" class="psp-title">用播放器打开</div>
            <div v-if="title" class="psp-name">{{ title }}</div>
          </div>
          <button type="button" class="psp-x" aria-label="关闭" @click="close">✕</button>
        </div>
        <p class="psp-sub">点一下，直接跳到播放器里填好</p>
        <div v-if="loading" class="psp-empty">正在读取播放器…</div>
        <p v-else-if="needsBinding" class="psp-empty">
          请先绑定自己的网盘账号才能点播。
          <RouterLink to="/game/account" @click="close">去“我的”页绑定网盘</RouterLink>
        </p>
        <p v-else-if="schemes.length === 0" class="psp-empty">
          这里还没有可导入的播放器。
          <RouterLink to="/game/operator" @click="close">去接线员页导入</RouterLink>
        </p>
        <div v-else class="psp-btns">
          <button
            v-for="s in schemes"
            :key="s.name"
            type="button"
            class="psp-btn"
            @click="pick(s)"
          >{{ s.name }}</button>
        </div>
        <p class="psp-tip">没装的话去<RouterLink to="/game/operator" @click="close">接线员页</RouterLink>导入</p>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { useToast } from '@/composables/useToast'
import { storageBindingApi } from '@/api/economy'
import {
  assignImportScheme,
  loadImportSchemes,
  type PlayerScheme,
} from '@/composables/usePlayerSchemes'

const open = defineModel<boolean>('open', { default: false })
defineProps<{ title?: string }>()

const toast = useToast()
const schemes = ref<PlayerScheme[]>([])
const loading = ref(false)
// 播放门禁引导：名下无 active 绑定时为 true， sheet 只展示绑定引导，
// 不直接跳播放器（跳过去也会在播放器里被服务端 403）。
const needsBinding = ref(false)
const panelEl = ref<HTMLElement | null>(null)
let jumpTimer = 0

watch(open, (now) => {
  if (!now) return
  void refresh()
  void nextTick(() => panelEl.value?.focus())
})

async function refresh() {
  loading.value = true
  try {
    const [schemesResult, bindingsResult] = await Promise.allSettled([
      loadImportSchemes(),
      storageBindingApi.list(),
    ])
    if (schemesResult.status === 'fulfilled') schemes.value = schemesResult.value
    // 绑定列表拉取失败时保持旧行为（服务端门禁仍会拦截），引导只是尽力而为。
    if (bindingsResult.status === 'fulfilled') {
      const bindings = bindingsResult.value?.bindings ?? []
      needsBinding.value = !bindings.some((b) => b.status === 'active')
    } else {
      needsBinding.value = false
    }
  } finally {
    loading.value = false
  }
}

function close() {
  window.clearTimeout(jumpTimer)
  open.value = false
}

async function pick(s: PlayerScheme) {
  // 跳转前先把提示留在页面上：没装 App 时 scheme 不会离开本页
  toast.info(`正在打开${s.name}。没装的话去接线员页导入`)
  await nextTick()
  await new Promise<void>((resolve) => {
    jumpTimer = window.setTimeout(resolve, 600)
  })
  if (!open.value) return
  if (!assignImportScheme(s.url)) toast.info('没装的话去接线员页导入')
}
</script>

<style scoped>
.psp {
  position: fixed;
  inset: 0;
  z-index: 90;
}
.psp-mask {
  position: absolute;
  inset: 0;
  border: 0;
  padding: 0;
  background: rgba(42, 38, 32, 0.45);
  cursor: pointer;
}
.psp-sheet {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  background: var(--paper-card, #fffdf6);
  color: var(--ink, #2a2620);
  border-top: 1.5px solid var(--ink, #2a2620);
  border-radius: 16px 16px 0 0;
  padding: 16px 16px calc(16px + env(safe-area-inset-bottom));
  max-height: min(70vh, 480px);
  overflow: auto;
}
.psp-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}
.psp-title {
  font-size: 16px;
  font-weight: 700;
  letter-spacing: 0.04em;
}
.psp-name {
  margin-top: 2px;
  font-size: 12px;
  color: var(--ink2, #6b6257);
}
.psp-x {
  border: 0;
  background: transparent;
  color: inherit;
  font-size: 16px;
  line-height: 1;
  min-width: 44px;
  min-height: 44px;
  cursor: pointer;
}
.psp-sub,
.psp-tip,
.psp-empty {
  font-size: 12px;
  color: var(--ink2, #6b6257);
  margin: 8px 0 0;
}
.psp-empty a,
.psp-tip a {
  color: var(--ink, #2a2620);
  font-weight: 700;
}
.psp-btns {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 14px;
}
.psp-btn {
  font-size: 14px;
  font-weight: 800;
  padding: 12px 20px;
  min-height: 44px;
  border-radius: var(--paper-r-lg, 12px);
  border: 2px solid var(--ink, #2a2620);
  background: var(--paper-card, #fffdf6);
  color: var(--ink, #2a2620);
  cursor: pointer;
  box-shadow: 0 2px 0 rgba(0, 0, 0, 0.12);
}
.psp-btn:active { transform: translateY(2px); box-shadow: none; }
.psp-btn:focus-visible,
.psp-x:focus-visible,
.psp-sheet:focus-visible {
  outline: 2px solid var(--red, #c23b22);
  outline-offset: 2px;
}
@media (min-width: 1024px) {
  .psp-sheet {
    left: 50%;
    right: auto;
    bottom: auto;
    top: 50%;
    transform: translate(-50%, -50%);
    width: min(420px, calc(100vw - 48px));
    border: 1.5px solid var(--ink, #2a2620);
    border-radius: 16px;
    padding: 20px;
  }
}
@media (prefers-reduced-motion: reduce) {
  .psp-btn:active { transform: none; }
}
</style>
