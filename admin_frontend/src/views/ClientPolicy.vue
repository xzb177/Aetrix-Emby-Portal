<script setup lang="ts">
/**
 * 客户端策略（v2.26.0）
 *
 * 一句话回答「客户端这边到底允许做什么」：
 *
 * - **下载与设备**：是否允许下载、每人几台设备、超限是拒绝还是自动踢最久未用的、
 *   安全日志保留多久（这组 SystemConfig 键的唯一写入口，「系统设置」只读回显）。
 *
 * 策略落在 SystemConfig 里，EM 与 EA 共用同一个库 → 面板上改完，出流的 EA 立刻生效。
 * 判定口径：**管理员不受限**（排障时不能被自己的策略挡住）；所有开关缺省 = 与升级前一致。
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { toAdminError } from '@/utils/errorMessage'
import {
  Activity, AlertTriangle, Download, Gauge, Info, RefreshCw, Save,
  Server, Smartphone, Zap,
} from 'lucide-vue-next'
import { AdvancedSettings, EmptyState, PageHeader, SectionCard, StatTile } from '@/components/ui'
import {
  fetchPlayLines, fetchPlaybackPolicy,
  updatePlaybackPolicy,
} from '@/api/admin'
import { fmtBytes } from '@/composables/useDangerOps'
// 下载与设备风控落在经济设置里：这批 SystemConfig 键的唯一写入口（系统设置页只读回显）
import { fetchEconomySettings, updateEconomySettings } from '@/api/economy'
import type {
  PlaybackPolicy, PlaybackRuntime,
  PlayLinesSnapshot,
} from '@/types'
import { useAuthStore } from '@/stores/auth'
import NoticePanel from '@/components/NoticePanel.vue'

const auth = useAuthStore()
// 这两组策略都由服务端限定为超级管理员（见 backend/admin_roles.py 的前缀规则），
// 所以只有一个口径：不是 super 就只读
const isSuper = computed(() => auth.admin?.is_super !== false)

const loading = ref(false)
/** 主策略读不到（整页没法展示）时显示错误态 */
const loadError = ref(false)
const savingPolicy = ref(false)
const savingOps = ref(false)

const policy = ref<PlaybackPolicy>({
  drive_direct_play_enabled: true,
})
const runtime = ref<PlaybackRuntime | null>(null)

/** 下载与设备风控：这批 SystemConfig 键的唯一写入口（系统设置页只读回显） */
const ops = ref({ allow_download: 'true', device_limit_per_user: '0', device_limit_auto_evict: 'false', login_log_retention_days: '' })
/** 下载与设备配置没读到：表单里不是生效值，禁止保存（否则会用空值覆盖线上配置） */
const opsError = ref(false)


const PLAYBACK_NODE_LABEL: Record<string, string> = {
  ea: '分离部署的 EA 节点',
  external: '已有 Emby 服',
  panel: '面板自己（一体化）',
}

async function load() {
  loading.value = true
  loadError.value = false
  try {
    const [p, s, ln] = await Promise.all([
      fetchPlaybackPolicy(),
      fetchEconomySettings().catch(() => null),
      fetchPlayLines().catch(() => null),
    ])
    policy.value = p.policy
    runtime.value = p.runtime
    playLines.value = ln
    opsError.value = !s
    if (s) {
      ops.value = {
        allow_download: s.settings.allow_download ?? '',
        device_limit_per_user: s.settings.device_limit_per_user ?? '',
        device_limit_auto_evict: s.settings.device_limit_auto_evict ?? '',
        login_log_retention_days: s.settings.login_log_retention_days ?? '',
      }
    }
  } catch {
    /* 拦截器已提示；运行态都没拿到说明主策略没读到 */
    loadError.value = !runtime.value
  } finally {
    loading.value = false
  }
}

onMounted(load)

// ==================== 播放线路可观测（Phase 3） ====================
// 播放路径只读展示。这里**不改任何线路配置**，只回答每条线路能不能用。
// 只回答“这会儿每条线路怎么样”：能不能用、是不是在降级、有多少人多少流量、效果如何。

const playLines = ref<PlayLinesSnapshot | null>(null)

/** 本机文件整文件直发不走本服务响应体，流量口径不包含它们 */
const lineBytesHint = '流量 = 本进程经手的出流量（不含整文件直发）'

async function savePolicy() {
  savingPolicy.value = true
  try {
    const res = await updatePlaybackPolicy(policy.value)
    policy.value = res.policy
    ElMessage.success('播放策略已保存（出流的节点下次判定即生效）')
  } catch (e) {
    ElMessage.error(toAdminError(e, '保存失败'))
  } finally {
    savingPolicy.value = false
  }
}

async function saveOps() {
  if (opsError.value) return
  savingOps.value = true
  try {
    await updateEconomySettings({ ...ops.value })
    ElMessage.success('下载与设备策略已保存')
  } catch (e) {
    ElMessage.error(toAdminError(e, '保存失败'))
  } finally {
    savingOps.value = false
  }
}

</script>

<template>
  <div class="admin-page client-policy">
    <PageHeader
      eyebrow="安全与准入"
      title="客户端策略"
      description="下载与设备、播放线路：改完对面板与出流的节点同时生效（管理员不受这些策略限制）。"
    >
      <template #actions>
        <el-button :loading="loading" @click="load">
          <RefreshCw :size="14" class="btn-ico" />刷新
        </el-button>
      </template>
    </PageHeader>

    <el-alert v-if="!isSuper" type="warning" :closable="false" show-icon>
      <template #title>保存需要超级管理员角色</template>
      <template #default>你可以查看当前策略，但保存会被服务端拒绝（这些策略会改变所有人的播放体验）。</template>
    </el-alert>

    <!-- 首屏骨架 -->
    <div v-if="loading && !runtime" class="cp-skeleton" aria-busy="true" aria-label="加载中">
      <div v-for="n in 5" :key="n" class="au-skeleton sk-tile" />
      <div class="au-skeleton sk-wide" />
    </div>

    <el-alert
      v-else-if="loadError"
      type="error"
      show-icon
      :closable="false"
      title="播放策略加载失败"
      description="下面显示的是默认值，请勿直接保存。点右上角「刷新」重试。"
    />

    <!-- 运行态 -->
    <SectionCard
      v-if="runtime"
      title="运行态"
      :icon="Gauge"
      meta="本进程"
      description="当前播放由谁出流（面板本机判定）。"
    >
      <div class="tile-grid">
        <StatTile
          label="当前出流"
          :icon="Server"
          :value="PLAYBACK_NODE_LABEL[runtime.playback_node] || runtime.playback_node"
          class="text-tile"
        />
      </div>
    </SectionCard>

    <!-- 下载与设备：这批配置键的唯一写入口（系统设置页只读回显） -->
    <SectionCard title="下载与设备" :icon="Download" description="下载与设备风控的唯一配置入口，在本页直接修改并保存。">
      <el-alert
        v-if="opsError"
        type="error"
        show-icon
        :closable="false"
        class="cp-ops-alert"
        title="下载与设备配置读取失败"
        description="下面不是当前生效值，已禁止保存；点右上角「刷新」重新读取。"
      />
      <div class="cp-field">
        <div class="cp-field-main">
          <label class="cp-label">允许下载</label>
          <p class="cp-hint">
            关闭后客户端下载（含 <code>/Items/{id}/File</code>）一并拦截，管理员不受限。
          </p>
        </div>
        <el-switch
          :model-value="ops.allow_download === 'true'"
          :disabled="!isSuper"
          @update:model-value="(v: string | number | boolean) => (ops.allow_download = v ? 'true' : 'false')"
        />
      </div>

      <div class="cp-field">
        <div class="cp-field-main">
          <label class="cp-label">设备上限（台 / 人）</label>
          <p class="cp-hint">0 或不填表示不限；只统计近 30 天活跃设备。</p>
        </div>
        <el-input v-model="ops.device_limit_per_user" class="cp-num" :disabled="!isSuper" />
      </div>

      <!-- 配置瘦身第 3 批：低频调优项收进高级折叠（默认收起） -->
      <AdvancedSettings
        count="2 项"
        description="调了也不影响主流程的参数：设备超限时的自动踢人策略、日志保留期。"
      >
        <div class="cp-field">
          <div class="cp-field-main">
            <label class="cp-label">超限自动踢人</label>
            <p class="cp-hint">
              开启则自动移除最久未使用的设备；关闭则直接拒绝新设备登录。
            </p>
          </div>
          <el-switch
            :model-value="ops.device_limit_auto_evict === 'true'"
            :disabled="!isSuper"
            @update:model-value="(v: string | number | boolean) => (ops.device_limit_auto_evict = v ? 'true' : 'false')"
          />
        </div>

        <div class="cp-field">
          <div class="cp-field-main">
            <label class="cp-label">日志保留（天）</label>
            <p class="cp-hint">登录与安全日志的保留期，未设置时默认 90 天。</p>
          </div>
          <el-input v-model="ops.login_log_retention_days" class="cp-num" :disabled="!isSuper" />
        </div>
      </AdvancedSettings>

      <template #footer>
        <div class="cp-save">
          <span class="cp-save-hint cp-inline-icon">
            <Smartphone :size="13" /> 逐台设备与登录日志在「设备与安全 / 登录日志」；这里只决定「多一台设备时怎么处理」。
          </span>
          <el-button
            type="primary"
            :loading="savingOps"
            :disabled="!isSuper || opsError"
            :title="opsError ? '配置读取失败，请先刷新再保存' : ''"
            @click="saveOps"
          >
            <Save :size="14" class="btn-ico" />保存
          </el-button>
        </div>
      </template>
    </SectionCard>

    <!-- 播放可观测（2026-10 简化）：单路径，只读 -->
    <SectionCard
      title="播放路径"
      :icon="Activity"
      :meta="playLines ? '302 直链' : (loading ? '加载中…' : '')"
      :description="playLines?.scope_note || lineBytesHint"
    >
      <p class="cp-hint cp-lead">
        播放统一走 302 直链：Drive 文件 302 到谷歌直链，挂载源（115/WebDAV/AList）
        302 到挂载解析出的直链，本机文件直接流——字节不经服务器。
      </p>

      <div v-if="playLines" class="line-card">
        <div class="line-head">
          <span class="au-badge au-badge-amber">302 直链</span>
          <p class="line-summary">{{ playLines.summary }}</p>
        </div>
        <div class="tile-grid">
          <StatTile label="播放请求（本进程）" :value="playLines.requests" />
          <StatTile label="出流量（本进程）" :value="fmtBytes(playLines.bytes_out)" />
        </div>
      </div>
      <EmptyState
        v-else-if="!loading"
        compact
        :icon="Activity"
        title="播放路径数据读取失败"
        description="下方策略与配置不受影响；点右上角「刷新」重试。"
      />
    </SectionCard>

    <!-- Drive 302 直链总开关（#957 的回滚闸） -->
    <SectionCard title="Drive 302 直链" :icon="Zap">
      <template #actions>
        <span class="au-badge" :class="policy.drive_direct_play_enabled ? 'au-badge-green' : 'au-badge-muted'">
          {{ policy.drive_direct_play_enabled ? '已开启' : '已关闭' }}
        </span>
      </template>

      <div class="cp-field">
        <div class="cp-field-main">
          <label class="cp-label" for="cp-drive-direct">Drive 文件 302 直链</label>
          <p class="cp-hint">
            打开（默认）：Drive 文件直接 302 到谷歌直链，播放器自拉，字节不经服务器。
            关闭：Drive 文件播放返回 410。这是 302 上线期的回滚闸——如果大量用户
            报播放 403（分享权限还没开完），可临时关闭止血。
          </p>
        </div>
        <el-switch id="cp-drive-direct" v-model="policy.drive_direct_play_enabled" :disabled="!isSuper" />
      </div>

      <template #footer>
        <div class="cp-save">
          <span class="cp-save-hint">本页所有开关同属播放策略，保存任一处都会一起提交。</span>
          <el-button type="primary" :loading="savingPolicy" :disabled="!isSuper" @click="savePolicy">
            <Save :size="14" class="btn-ico" />保存
          </el-button>
        </div>
      </template>
    </SectionCard>


    <NoticePanel
      title="为什么这些策略在这里，而系统设置在别处"
      summary="客户端能力与站点运营设置各归其位"
      :icon="Info"
      storage-key="client-policy-scope"
    >
      <div class="policy-guide">
        这里只放「客户端能做什么」（下载与设备）；
        「站点怎么运营」（注册、支付、付费墙、邀请返利）仍然在系统设置里。
      </div>
    </NoticePanel>

    <p v-if="!isSuper" class="readonly-foot">
      <AlertTriangle :size="13" />
      当前账号不是超级管理员：这一页可以查看，保存会被服务端拒绝（这些策略会改变所有人的播放体验）。
    </p>
  </div>
</template>

<style scoped>
.client-policy { gap: 16px; }
.btn-ico { margin-right: 4px; }
.policy-guide { line-height: 1.7; }

/* ===== 骨架 / 瓦片网格 ===== */
.cp-skeleton {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px;
}
.sk-tile { height: 92px; border-radius: var(--au-r-lg); }
.sk-wide { grid-column: 1 / -1; height: 260px; border-radius: var(--au-r-lg); }
.tile-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px;
}
/* 值是短文字（可用 / 面板自己）：字号收一档 */
.text-tile :deep(.au-stat__value) { font-size: 1.05rem; }

/* ===== 表单行：左说明、右控件 ===== */
.cp-field {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 14px 0;
  border-bottom: 1px solid var(--au-border);
}
.cp-field:first-child { padding-top: 0; }
.cp-field:last-child { border-bottom: none; padding-bottom: 0; }
.cp-field.is-column { flex-direction: column; align-items: stretch; gap: 8px; }
.cp-field-main { min-width: 0; flex: 1 1 auto; }
.cp-label { font-size: 13.5px; font-weight: 500; color: var(--au-text); }
.cp-hint { margin: 4px 0 0; font-size: 12px; color: var(--au-text-3); line-height: 1.7; }
.cp-lead { margin: 0 0 14px; color: var(--au-text-2); }
.cp-stats-foot { margin: 10px 0 6px; }
.cp-value { font-size: 12px; color: var(--au-text-2); text-align: right; word-break: break-all; }
.cp-unit { font-size: 12px; color: var(--au-text-3); white-space: nowrap; }
.cp-num { width: 140px; flex: none; }
.cp-input { width: 280px; flex: none; }

/* 热门判定规则的两个数字框：并排时不被压扁，窄屏允许换行 */
.num-pair { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; min-width: 0; }
.num-pair .el-input-number { flex: none; width: 120px; }

/* ===== 卡片底部保存栏：说明左、按钮右 ===== */
.cp-ops-alert { margin-bottom: 12px; }
.cp-save {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.cp-save-hint { flex: 1 1 240px; }
/* Batch D 入口收敛：页内互链（UA 两处 / 下载与设备只读提示） */
.cp-link { color: var(--au-accent, #b45309); font-weight: 700; text-decoration: none; }
.cp-link:hover { text-decoration: underline; }
.cp-inline-icon { display: inline-flex; align-items: center; gap: 6px; }
.cp-save-left { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.danger-jump {
  color: var(--au-text-3);
  text-decoration: none;
  font-size: 12px;
}
.danger-jump:hover { color: var(--au-danger); text-decoration: underline; }

/* ===== 播放路径 ===== */
.line-card {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 14px;
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-md);
  background: var(--au-bg-soft);
}
.line-head { display: flex; align-items: flex-start; gap: 10px; flex-wrap: wrap; }
.line-summary { margin: 0; flex: 1 1 240px; font-size: 12.5px; color: var(--au-text-2); line-height: 1.7; }
.line-ready {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 0;
  padding: 0;
  list-style: none;
  font-size: 12px;
  line-height: 1.7;
  color: var(--au-text-3);
}
.line-key { display: inline-block; min-width: 64px; color: var(--au-text-2); font-weight: 500; }

/* ===== 本地缓存条目 ===== */
.cache-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 14px;
  padding-top: 14px;
  border-top: 1px solid var(--au-border);
}
.cache-list-head { font-size: 12px; color: var(--au-text-3); margin-bottom: 2px; }
.cache-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 10px;
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-sm);
  background: var(--au-surface-2);
}
.cache-item .ci-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  color: var(--au-text);
}
.cache-item .ci-meta {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
  font-size: 12px;
  color: var(--au-text-3);
  font-variant-numeric: tabular-nums;
}

code {
  padding: 1px 5px;
  border-radius: var(--au-r-sm);
  background: var(--au-surface-2);
  font-family: var(--font-mono);
  font-size: 11.5px;
}
.readonly-foot {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0;
  font-size: 12px;
  color: var(--au-warning);
}

@media (max-width: 768px) {
  /* 左说明右控件 → 上下排，控件铺满 */
  .cp-field { flex-direction: column; align-items: stretch; gap: 10px; }
  .cp-field > .el-switch { align-self: flex-start; }
  .cp-num,
  .cp-input { width: 100%; }
  .cp-value { text-align: left; }
  .cache-item { flex-direction: column; align-items: flex-start; gap: 4px; }
  .cache-item .ci-name { max-width: 100%; }
  .cache-item .ci-meta { flex-wrap: wrap; }
  .cp-save > .el-button { flex: 1 1 100%; }
  .cp-save-left { flex: 1 1 100%; }
  .cp-save-left .el-button { flex: 1 1 0; margin-left: 0; }
  /*
    窄屏下响应式层会把 .el-input-number 统一拉成 100%：两个数字框挤在同一行里会
    互相抢宽度，数字被两侧的加减按钮盖住。这里让它们各占可读的最小宽度。
  */
  .num-pair { width: 100%; }
  .num-pair .el-input-number {
    flex: 1 1 112px;
    width: auto !important;
    min-width: 112px;
  }
}
</style>
