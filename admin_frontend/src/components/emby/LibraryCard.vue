<script setup lang="ts">
import { Film, Server, HardDrive, ImagePlus, X, ScanSearch, RefreshCcw, RefreshCw, Search, Settings2, History, Delete } from 'lucide-vue-next'
import type { UploadRequestOptions } from 'element-plus'

defineProps<{
  library: any
  coverUrl?: string
  typeLabel: string
  badge?: { cls: string; text: string } | null
  serviceText: string
  serviceWarn?: boolean
  sourceText: string
  sourceWarn?: boolean
  summary: string
  summaryTitle?: string
  liveHint?: string
  liveState?: string
  lastScanText: string
  scanError?: string
  reachMessage?: string
  coverUploading?: boolean
}>()

const emit = defineEmits<{
  (e: 'cover-upload', options: UploadRequestOptions): void
  (e: 'cover-remove'): void
  (e: 'scan'): void
  (e: 'scan-full'): void
  (e: 'rescrape'): void
  (e: 'identify'): void
  (e: 'settings'): void
  (e: 'scans'): void
  (e: 'remove'): void
}>()
</script>

<template>
  <article class="lib-card">
    <div class="lib-cover">
      <img v-if="coverUrl" :src="coverUrl" :alt="`${library.name} 封面`" loading="lazy" decoding="async" />
      <div v-else class="lib-cover-empty">
        <Film :size="32" />
        <span>{{ typeLabel }}库</span>
      </div>
      <div class="lib-cover-shade" />
      <div class="lib-cover-badges">
        <span v-if="library.is_virtual" class="mini-badge pin">虚拟库</span>
        <span class="mini-badge" :class="library.is_enabled ? 'ok' : 'off'">
          {{ library.is_enabled ? '启用' : '停用' }}
        </span>
        <span v-if="badge" class="mini-badge" :class="badge.cls">{{ badge.text }}</span>
      </div>
      <div class="lib-cover-actions">
        <el-upload
          accept="image/jpeg,image/png,image/webp"
          :show-file-list="false"
          :disabled="coverUploading"
          :http-request="(options: UploadRequestOptions) => emit('cover-upload', options)"
        >
          <el-button class="cover-button" circle text :loading="coverUploading" title="上传封面" aria-label="上传媒体库封面">
            <ImagePlus :size="18" />
          </el-button>
        </el-upload>
        <el-button v-if="library.cover_url" class="cover-button" circle text title="移除封面" @click="emit('cover-remove')">
          <X :size="17" />
        </el-button>
      </div>
    </div>

    <div class="lib-body">
      <div class="lib-head">
        <span class="lib-name">{{ library.name }}</span>
        <span class="lib-meta">{{ typeLabel }}库</span>
      </div>
      <div class="lib-facts">
        <span class="fact" :class="{ warn: serviceWarn }" :title="'服务：' + serviceText">
          <Server :size="12" />{{ serviceText }}
        </span>
        <span class="fact" :class="{ warn: sourceWarn }" :title="'来源：' + sourceText">
          <HardDrive :size="12" />{{ sourceText }}
        </span>
        <span class="fact"><Film :size="12" />{{ library.item_count }} 个条目</span>
      </div>
      <div class="lib-summary" :title="summaryTitle">{{ summary }}</div>
      <div class="lib-state">
        <span v-if="liveHint" class="lib-live" :title="liveHint">
          <span class="scan-dot" :class="liveState === 'queued' ? 'is-queued' : 'is-running'" />
          {{ liveHint }}
        </span>
        <span v-else class="lib-time">{{ lastScanText }}</span>
      </div>
      <div v-if="scanError" class="scan-error" :title="scanError">{{ scanError }}</div>
      <div v-else-if="reachMessage" class="scan-error">播放风险：{{ reachMessage }}</div>
    </div>

    <div class="lib-foot">
      <el-button size="small" type="primary" plain title="增量扫描" @click="emit('scan')">
        <ScanSearch :size="13" />扫描
      </el-button>
      <el-button size="small" plain title="全量扫描" @click="emit('scan-full')">
        <RefreshCcw :size="13" />全量扫描
      </el-button>
      <el-button size="small" plain title="刷新元数据" @click="emit('rescrape')">
        <RefreshCw :size="13" />刷新元数据
      </el-button>
      <el-button size="small" plain title="识别" @click="emit('identify')">
        <Search :size="13" />识别
      </el-button>
      <el-button size="small" plain @click="emit('settings')">
        <Settings2 :size="13" />设置
      </el-button>
      <el-button size="small" plain title="扫描记录" @click="emit('scans')">
        <History :size="13" />
      </el-button>
      <el-button size="small" type="danger" plain title="删除媒体库" @click="emit('remove')">
        <Delete :size="13" />
      </el-button>
    </div>
  </article>
</template>

<style scoped>
.lib-card {
  content-visibility: auto;
  contain-intrinsic-size: auto 320px;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--el-border-color);
  border-radius: 8px;
  overflow: hidden;
  background: var(--el-bg-color);
}
.lib-cover { position: relative; aspect-ratio: 16 / 9; background: var(--el-fill-color-light); }
.lib-cover img { width: 100%; height: 100%; object-fit: cover; }
.lib-cover-empty { display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100%; color: var(--el-text-color-secondary); }
.lib-cover-shade { position: absolute; inset: 0; background: linear-gradient(to top, rgba(0,0,0,.35), transparent 50%); pointer-events: none; }
.lib-cover-badges { position: absolute; top: 8px; left: 8px; display: flex; gap: 4px; }
.mini-badge { padding: 2px 6px; border-radius: 4px; font-size: 11px; background: rgba(0,0,0,.55); color: #fff; }
.mini-badge.ok { background: var(--el-color-success); }
.mini-badge.off { background: var(--el-color-info); }
.lib-cover-actions { position: absolute; top: 8px; right: 8px; display: flex; gap: 4px; }
.lib-body { padding: 12px; display: flex; flex-direction: column; gap: 8px; }
.lib-head { display: flex; justify-content: space-between; align-items: center; }
.lib-name { font-weight: 600; }
.lib-meta { font-size: 12px; color: var(--el-text-color-secondary); }
.lib-facts { display: flex; flex-wrap: wrap; gap: 8px; font-size: 12px; }
.fact.warn { color: var(--el-color-warning); }
.lib-summary { font-size: 12px; color: var(--el-text-color-secondary); }
.lib-foot { display: flex; flex-wrap: wrap; gap: 6px; padding: 0 12px 12px; }
.scan-error { font-size: 12px; color: var(--el-color-danger); }
.scan-dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: var(--el-color-success); margin-right: 4px; }
.scan-dot.is-queued { background: var(--el-color-warning); }
</style>
