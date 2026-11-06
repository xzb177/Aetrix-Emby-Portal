<template>
  <div class="pc-entry">
    <PcEntryRow
      :icon="Smartphone"
      name="我的设备"
      :meta="metaText"
      expandable
      :expanded="open"
      @click="open = !open"
    />
    <div v-if="open" class="pc-expand">
      <div class="dev-head">
        <span v-if="deviceInfo && deviceInfo.limit" class="dev-count">
          {{ deviceInfo.active_count ?? 0 }} / {{ deviceInfo.limit }} 台
        </span>
        <span v-else class="dev-count"></span>
        <button type="button" class="v4-mini-btn" title="刷新" :disabled="deviceLoading" :aria-busy="deviceLoading" @click="loadDevices">
          <RefreshCw :size="13" /> {{ deviceLoading ? '刷新中' : '刷新' }}
        </button>
      </div>
      <div v-if="deviceLoading && !devices.length" class="v4-skeleton" style="height:120px"></div>
      <div v-else-if="deviceError" class="v4-err">设备列表加载失败，请稍后重试<br><button class="v4-retry" @click="loadDevices">重试</button></div>
      <div v-else-if="!devices.length" class="v4-empty">
        还没有设备记录 —— 用 Emby 客户端（Infuse / Forward 等）登录后会出现在这里
      </div>
      <template v-else>
        <div v-for="d in devices" :key="d.device_id" class="dev-row">
          <div class="dev-main">
            <span class="dev-name">{{ d.name || '未命名设备' }}</span>
            <span class="dev-meta">
              {{ d.client || '未知客户端' }}
              <template v-if="d.app_version"> v{{ d.app_version }}</template>
              · {{ deviceAgo(d.last_seen_at, d.is_blocked) }}
              <template v-if="d.ip"> · {{ d.ip }}</template>
            </span>
          </div>
          <span v-if="d.is_blocked" class="dev-badge off">已禁用</span>
          <span v-else-if="!d.is_online_recent" class="dev-badge idle">已闲置</span>
          <span v-else class="dev-badge ok">活跃</span>
          <button
            type="button"
            class="dev-remove"
            :class="{ confirming: pendingRemove === d.device_id }"
            :disabled="removingDevice === d.device_id"
            @click="handleRemoveDevice(d)"
          >
            {{ removingDevice === d.device_id ? '移除中' : (pendingRemove === d.device_id ? '确认移除' : '移除') }}
          </button>
        </div>
      </template>
      <p class="dev-tip">
        <template v-if="deviceInfo && deviceInfo.limit">
          账号最多同时使用 {{ deviceInfo.limit }} 台设备（近 {{ deviceInfo.active_days }} 天内登录过）。
          {{ deviceInfo.auto_evict ? '超限时会自动停用最久未用的设备。' : '超限时请先移除不再使用的设备。' }}
        </template>
        <template v-else>移除后该设备登录状态立即失效，需重新输入账号密码。</template>
      </p>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 我的设备（自旧 AccountSettingsView 原样抽出）：设备列表 / 两步确认移除，
 * 顶上限自救通道。挂载即加载（入口行右侧小字要显示 N / M 台，属现有接口
 * 拿得到的摘要）；加载失败不装成"没设备"，展开里留错误态重试。
 */
import { computed, onMounted, ref } from 'vue'
import { RefreshCw, Smartphone } from 'lucide-vue-next'
import PcEntryRow from './PcEntryRow.vue'
import { deviceApi, type MyDevice, type MyDevicesResponse } from '@/api/economy'
import { useToast } from '@/composables/useToast'
import { humanizeError } from '@/composables/usePanelApi'
import { toastCopy } from '@/constants/toastMessages'
import { toUserMessage } from '@/utils/toUserMessage'
import { parseServerTime } from '@/utils/serverTime'

const toast = useToast()
const open = ref(false)
const devices = ref<MyDevice[]>([])
const deviceInfo = ref<MyDevicesResponse | null>(null)
const deviceLoading = ref(false)
const deviceError = ref('')
const removingDevice = ref('')
const pendingRemove = ref('')

const metaText = computed(() => {
  if (!deviceInfo.value || !deviceInfo.value.limit) return ''
  return `${deviceInfo.value.active_count ?? 0} / ${deviceInfo.value.limit} 台`
})

async function loadDevices() {
  deviceLoading.value = true
  deviceError.value = ''
  try {
    const res = await deviceApi.mine()
    devices.value = res.devices
    deviceInfo.value = res
  } catch (e) {
    deviceError.value = humanizeError(e)
  } finally {
    deviceLoading.value = false
  }
}

async function handleRemoveDevice(device: MyDevice) {
  if (removingDevice.value) return
  if (pendingRemove.value !== device.device_id) {
    pendingRemove.value = device.device_id
    window.setTimeout(() => {
      if (pendingRemove.value === device.device_id) pendingRemove.value = ''
    }, 4000)
    return
  }
  pendingRemove.value = ''
  removingDevice.value = device.device_id
  try {
    const res = await deviceApi.remove(device.device_id)
    toast.success(res.message || toastCopy.deviceRemoved())
    await loadDevices()
  } catch (err: any) {
    toast.error(toUserMessage(err, 'removeFailed'))
  } finally {
    removingDevice.value = ''
  }
}

function deviceAgo(iso?: string | null, isBlocked = false) {
  if (isBlocked) return '已禁用'
  if (!iso) return '—'
  // PA-047：last_seen_at 是 UTC naive 串，须按 UTC 解析，否则时长多算 8 小时
  const diff = Date.now() - parseServerTime(iso).getTime()
  if (Number.isNaN(diff)) return '—'
  if (diff < 0) return '刚刚'
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return '刚刚'
  if (mins < 60) return `${mins} 分钟前`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours} 小时前`
  return `${Math.floor(hours / 24)} 天前`
}

onMounted(() => {
  void loadDevices()
})
</script>

<style scoped>
.pc-expand {
  border-top: 1px dashed var(--line);
  padding: 12px 14px 14px;
}
.dev-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
}
.dev-count {
  font-size: 12px;
  color: var(--ink2);
  font-variant-numeric: tabular-nums;
}
.v4-mini-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 15px 10px;
  margin: -8px 0;
  background: var(--paper2);
  border: 1px solid var(--line);
  border-radius: var(--paper-r-full, 999px);
  color: var(--ink2);
  font-size: 12px;
  cursor: pointer;
}
.dev-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 0;
}
.dev-row + .dev-row { border-top: 1px solid var(--line); }
.dev-main {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  flex: 1;
}
.dev-name {
  font-size: 14px;
  font-weight: 500;
  color: var(--ink);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.dev-meta {
  font-size: 12px;
  color: var(--ink2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
.dev-badge {
  flex-shrink: 0;
  padding: 2px 8px;
  border-radius: var(--paper-r-full, 999px);
  font-size: 12px;
  white-space: nowrap;
  background: var(--paper2);
  color: var(--ink2);
}
.dev-badge.ok { background: var(--au-success-soft); color: var(--green); }
.dev-badge.off { background: var(--au-danger-soft); color: var(--red); }
.dev-remove {
  flex-shrink: 0;
  padding: 15px 10px;
  margin: -7px 0;
  background: transparent;
  border: none;
  border-radius: var(--paper-r-sm, 8px);
  color: var(--ink2);
  font-size: 13px;
  white-space: nowrap;
  cursor: pointer;
}
.dev-remove:active:not(:disabled) { background: var(--paper2); }
.dev-remove:disabled { opacity: 0.5; cursor: not-allowed; }
.dev-remove.confirming { color: var(--red); font-weight: 600; }
.dev-tip {
  margin: 10px 0 0;
  padding-top: 10px;
  border-top: 1px solid var(--line);
  font-size: 12px;
  line-height: 1.6;
  color: var(--ink2);
}
</style>
