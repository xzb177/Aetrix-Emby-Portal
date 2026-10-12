<script setup lang="ts">
import { ListChecks, X } from 'lucide-vue-next'
import SectionCard from '@/components/ui/SectionCard.vue'
import EmptyState from '@/components/ui/EmptyState.vue'

defineProps<{
  scanQueue: any
  queueHasContent: boolean
  queueHistory: any[]
  showScanHistory: boolean
  progressLine: (t: any) => string
  waitingText: (t: any) => string
  queuedSince: (t: any) => string
  triggerLabel: (t: string) => string
  taskBadge: (t: any) => { cls: string; text: string }
  scanCountText: (t: any) => string
  fmtDuration: (ms: number) => string
}>()

const emit = defineEmits<{
  (e: 'update:showScanHistory', v: boolean): void
  (e: 'cancel', t: any): void
}>()
</script>

<template>
  <SectionCard v-if="queueHasContent" class="queue-card" title="扫描队列" :icon="ListChecks">
    <template #actions>
      <div class="queue-facts">
        <span class="fact">并发上限 {{ scanQueue?.max_parallel }}</span>
        <span class="fact">{{ scanQueue?.mount_serial ? '同一远程挂载串行' : '挂载串行已关闭' }}</span>
        <span class="fact" title="本轮真实远程请求 / 内存复用 / 在飞请求">
          远程请求 {{ scanQueue?.remote.lists }} · 复用 {{ scanQueue?.remote.reused }}
          · 在飞 {{ scanQueue?.remote.inflight }}
        </span>
        <span v-if="scanQueue?.view === 'db'" class="fact" title="进度来自执行节点">进度来自执行节点</span>
      </div>
    </template>

    <div class="queue-grid">
      <div class="queue-col">
        <div class="queue-col-title">正在扫描（{{ scanQueue?.running.length || 0 }}）</div>
        <div v-for="t in scanQueue?.running" :key="'run-' + t.library_id" class="queue-row">
          <div class="queue-row-head">
            <span class="queue-name">{{ t.name }}</span>
            <span class="mini-badge scanning">{{ t.progress?.phase_label || '准备中' }}</span>
            <span v-if="t.via === 'db'" class="mini-badge muted" title="执行节点">执行节点</span>
          </div>
          <div class="queue-row-sub mono">{{ progressLine(t) || '刚刚开始' }}</div>
          <div v-if="t.progress?.current" class="queue-row-sub mono" :title="t.progress.current">{{ t.progress.current }}</div>
        </div>
        <div v-if="!scanQueue?.running.length" class="queue-empty">没有正在跑的扫描</div>
      </div>

      <div class="queue-col">
        <div class="queue-col-title">排队中（{{ scanQueue?.waiting.length || 0 }}）</div>
        <div v-for="t in scanQueue?.waiting" :key="'wait-' + t.library_id" class="queue-row">
          <div class="queue-row-head">
            <span class="queue-name">{{ t.name }}</span>
            <span class="mini-badge muted">第 {{ t.position ?? '-' }} 位</span>
            <el-button size="small" text :icon="X" @click="emit('cancel', t)">取消</el-button>
          </div>
          <div class="queue-row-sub warn">{{ waitingText(t) }}</div>
          <div class="queue-row-sub mono">{{ [queuedSince(t), triggerLabel(t.trigger)].filter(Boolean).join(' · ') }}</div>
        </div>
        <div v-if="!scanQueue?.waiting.length" class="queue-empty">没有排队的扫描</div>
      </div>

      <div class="queue-col">
        <div class="queue-col-title queue-col-title--split">
          <span>最近完成</span>
          <el-switch :model-value="showScanHistory" size="small" @update:model-value="emit('update:showScanHistory', $event)" />
        </div>
        <template v-if="showScanHistory">
          <div v-for="t in queueHistory" :key="'done-' + t.library_id + t.requested_at" class="queue-row">
            <div class="queue-row-head">
              <span class="queue-name">{{ t.name }}</span>
              <span class="mini-badge" :class="taskBadge(t).cls">{{ taskBadge(t).text }}</span>
            </div>
            <div class="queue-row-sub mono">
              {{ [scanCountText(t), t.duration_ms != null ? `耗时 ${fmtDuration(t.duration_ms)}` : '', queuedSince(t), triggerLabel(t.trigger), t.request_count > 1 ? `被点 ${t.request_count} 次` : ''].filter(Boolean).join(' · ') }}
            </div>
            <div v-if="t.error" class="queue-row-sub danger" :title="t.error">{{ t.error }}</div>
          </div>
          <div v-if="!queueHistory.length" class="queue-empty">还没有跑完的扫描</div>
        </template>
      </div>
    </div>
  </SectionCard>
  <EmptyState
    v-else
    :icon="ListChecks"
    title="扫描队列是空的"
    description="没有正在跑或排队的扫描，也还没有跑完的记录。在「媒体库」标签点「扫描」或「一键扫描全部」后，进度会显示在这里。"
  />
</template>

<style scoped>
.queue-card { content-visibility: auto; contain-intrinsic-size: auto 240px; }
.queue-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }
.queue-col-title { font-weight: 600; margin-bottom: 8px; }
.queue-row { padding: 8px 0; border-bottom: 1px solid var(--el-border-color-lighter); }
.queue-row-head { display: flex; align-items: center; gap: 8px; }
.queue-name { font-weight: 500; }
.queue-row-sub { font-size: 12px; color: var(--el-text-color-secondary); }
.queue-empty { font-size: 13px; color: var(--el-text-color-secondary); padding: 8px 0; }
.mini-badge { padding: 1px 6px; border-radius: 4px; font-size: 11px; background: var(--el-fill-color); }
.mini-badge.scanning { background: var(--el-color-primary-light-9); color: var(--el-color-primary); }
@media (max-width: 900px) { .queue-grid { grid-template-columns: 1fr; } }
</style>
