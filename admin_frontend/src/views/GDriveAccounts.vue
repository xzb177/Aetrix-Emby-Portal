<script setup lang="ts">
/**
 * Google Drive 账号（Fix 6）
 *
 * 用户实际用的是 Google Drive（100 个 Google 子账号），不是 115。
 * 本页展示子账号状态：数量、当前轮换位置、磁盘压力、rclone 缓存大小，
 * 并支持手动触发子账号轮换。
 *
 * 版式与其它页一致：PageHeader + StatTile + SectionCard，按钮统一 el-button，
 * 颜色只用 --au-* 令牌（以前的 `.btn` / `.icon-btn` 没有全局样式，按钮是裸的）。
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { AlertTriangle, Cloud, HardDrive, RefreshCw, RotateCw, Gauge } from 'lucide-vue-next'
import { PageHeader, SectionCard, StatTile, EmptyState } from '@/components/ui'
import { fetchGDriveSaStatus, rotateGDriveSa } from '@/api/admin'

interface DiskPressure {
  ok?: boolean
  free_gb?: number
  total_gb?: number
  use_pct?: number
  warning?: string | null
}

interface SaStatus {
  sa_count: number
  sa_files: string[]
  sa_truncated: boolean
  current_sa: string | null
  rotation_index: number
  disk: DiskPressure | Record<string, unknown>
  cache_size_bytes: number
  cache_size_human: string
}

const loading = ref(false)
const status = ref<SaStatus | null>(null)
const loadError = ref('')
const rotating = ref(false)

/** 后端 check_disk_pressure 的固定字段；老后端没有这个函数时是空对象 */
const disk = computed<DiskPressure | null>(() => {
  const d = status.value?.disk as DiskPressure | undefined
  if (!d || typeof d !== 'object' || !('total_gb' in d)) return null
  return d
})

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    status.value = await fetchGDriveSaStatus()
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : '请求失败'
  } finally {
    loading.value = false
  }
}

async function rotate() {
  try {
    await ElMessageBox.confirm(
      '换下一个 Google 子账号继续读写文件，正在播放的影片不会中断。确定要换吗？',
      '手动轮换',
      { confirmButtonText: '换下一个账号', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  rotating.value = true
  try {
    const data = await rotateGDriveSa()
    if (data.needs_remount) {
      ElMessageBox.alert(data.remount_hint || '已改写 rclone 配置，需重新挂载 rclone 后才生效。', `已切换到 ${data.sa_file || '下一个账号'}（需重新挂载）`, { type: 'warning' }).catch(() => {})
    } else {
      ElMessage.success(`已切换到 ${data.sa_file || '下一个账号'}`)
    }
    await load()
  } catch {
    // 拦截器已提示（例如子账号目录不存在）
  } finally {
    rotating.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="admin-page">
    <PageHeader
      eyebrow="媒体与交付"
      title="Google Drive 账号"
      description="Google 子账号池（rclone 用它读写网盘）：当前用到第几个、缓存和磁盘还剩多少。某个账号被 Google 限流时，可以手动换下一个。"
    >
      <template #actions>
        <el-button :loading="loading" :icon="RefreshCw" @click="load">刷新</el-button>
        <el-button
          type="primary"
          :loading="rotating"
          :disabled="!status || !status.sa_count"
          :icon="RotateCw"
          @click="rotate"
        >手动轮换</el-button>
      </template>
    </PageHeader>

    <SectionCard v-if="loadError && !status">
      <EmptyState compact :icon="AlertTriangle" title="读取 Google 子账号状态失败" :description="loadError">
        <template #actions><el-button :loading="loading" @click="load">重试</el-button></template>
      </EmptyState>
    </SectionCard>

    <div v-else-if="!status" class="gd-skeleton" aria-busy="true" aria-label="加载中">
      <span class="au-skeleton" /><span class="au-skeleton" /><span class="au-skeleton" />
    </div>

    <template v-else>
      <div class="gd-stats">
        <StatTile label="Google 子账号数" :value="status.sa_count" :icon="Cloud" />
        <StatTile
          label="当前账号"
          :value="status.current_sa || '—'"
          :hint="status.current_sa ? `轮换位置 #${status.rotation_index}` : '还没有轮换记录'"
          :title="status.current_sa || ''"
          :icon="RotateCw"
          class="gd-current"
        />
        <StatTile label="rclone 缓存" :value="status.cache_size_human || '0 B'" :icon="HardDrive" />
        <StatTile
          v-if="disk"
          label="磁盘剩余"
          :value="`${disk.free_gb ?? 0} GB`"
          :hint="`共 ${disk.total_gb ?? 0} GB · 已用 ${disk.use_pct ?? 0}%`"
          :tone="disk.ok === false ? 'warn' : 'plain'"
          :icon="Gauge"
        />
      </div>

      <el-alert v-if="disk?.warning" type="warning" :closable="false" show-icon :title="disk.warning" />

      <SectionCard
        title="账号列表"
        :icon="Cloud"
        :meta="status.sa_truncated ? `前 20 / 共 ${status.sa_count}` : `${status.sa_count} 个`"
      >
        <ul v-if="status.sa_files.length" class="gd-list">
          <li
            v-for="f in status.sa_files"
            :key="f"
            class="gd-item"
            :class="{ 'is-active': f === status.current_sa }"
          >
            <span class="gd-name mono" :title="f">{{ f }}</span>
            <span v-if="f === status.current_sa" class="gd-badge">当前</span>
          </li>
        </ul>
        <EmptyState
          v-else
          compact
          :icon="Cloud"
          title="没有找到 Google 子账号"
          description="把 Google 子账号的 JSON 密钥文件放进服务器的 /opt/rclone-sa 目录，再点右上角「刷新」。"
        />
        <p v-if="status.sa_truncated" class="gd-hint">
          只列出前 20 个文件名；轮换会在全部 {{ status.sa_count }} 个账号里循环。
        </p>
      </SectionCard>
    </template>
  </div>
</template>

<style scoped>
.gd-stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
}
/* 子账号文件名很长：一行省略，悬停看全 */
.gd-current :deep(.au-stat__value) {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 1rem;
}

.gd-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.gd-item {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  padding: 8px 12px;
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-md);
  background: var(--au-bg-soft);
}
.gd-item.is-active {
  border-color: var(--au-primary-border);
  background: var(--au-primary-soft);
}
.gd-name {
  flex: 1;
  min-width: 0;
  font-size: 13px;
  color: var(--au-text-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.gd-item.is-active .gd-name { color: var(--au-text); }
.gd-badge {
  flex-shrink: 0;
  padding: 1px 8px;
  border: 1px solid var(--au-success-border);
  border-radius: var(--au-r-full);
  background: var(--au-success-soft);
  color: var(--au-success);
  font-size: 12px;
}
.gd-hint {
  margin: 12px 0 0;
  font-size: 12px;
  color: var(--au-text-3);
}
.gd-skeleton { display: flex; flex-direction: column; gap: 10px; }
.gd-skeleton .au-skeleton { display: block; height: 72px; border-radius: var(--au-r-lg); }
</style>
