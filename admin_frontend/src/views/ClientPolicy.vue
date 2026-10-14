<script setup lang="ts">
/**
 * 客户端策略（v2.26.0）
 *
 * 一句话回答「客户端这边到底允许做什么」：
 *
 * - **转码**：允不允许服务端转码、同时几路、码率上限（省 CPU / 省上行）；
 * - **客户端准入**：哪些 UA 不许进（老版本、盗版客户端），或反过来只允许白名单；
 * - **下载与设备**：是否允许下载、每人几台设备、超限是拒绝还是自动踢最久未用的、
 *   安全日志保留多久（这组 SystemConfig 键的唯一写入口，「系统设置」只读回显）。
 *
 * 策略落在 SystemConfig 里，EM 与 EA 共用同一个库 → 面板上改完，出流的 EA 立刻生效。
 * 判定口径：**管理员不受限**（排障时不能被自己的策略挡住）；所有开关缺省 = 与升级前一致。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import {
  Activity, AlertTriangle, Clock, Cloud, Cpu, Database, Download, Gauge, HardDrive, Info, RefreshCw, Save,
  Server, ShieldBan, Smartphone, Target, Trash2, Tv,
} from 'lucide-vue-next'
import { EmptyState, PageHeader, SectionCard, StatTile } from '@/components/ui'
import {
  fetchCdnConfig, fetchLocalCacheConfig, fetchPlayLines, fetchPlaybackPolicy,
  updateCdnConfig, updateLocalCacheConfig, updatePlaybackPolicy,
} from '@/api/admin'
import { useDangerOps, fmtBytes } from '@/composables/useDangerOps'
// 下载与设备风控落在经济设置里：这批 SystemConfig 键的唯一写入口（系统设置页只读回显）
import { fetchEconomySettings, updateEconomySettings } from '@/api/economy'
import type {
  CdnConfig, LocalCacheConfig, LocalCacheEntryInfo, LocalCacheStats, PlaybackPolicy, PlaybackRuntime,
  PlayLinesSnapshot,
} from '@/types'
import { useAuthStore } from '@/stores/auth'
import NoticePanel from '@/components/NoticePanel.vue'

const auth = useAuthStore()
/** 危险操作共用实现（与「系统设置 → 危险操作」页签同一份） */
const dangerOps = useDangerOps()
// 这两组策略都由服务端限定为超级管理员（见 backend/admin_roles.py 的前缀规则），
// 所以只有一个口径：不是 super 就只读
const isSuper = computed(() => auth.admin?.is_super !== false)

const loading = ref(false)
/** 主策略读不到（整页没法展示）时显示错误态 */
const loadError = ref(false)
const savingPolicy = ref(false)
const savingOps = ref(false)
const savingCdn = ref(false)
const savingCache = ref(false)
const cleaningCache = ref(false)

const policy = ref<PlaybackPolicy>({
  transcode_enabled: true,
  max_concurrent_transcodes: 0,
  blocked_agents: '',
  allowed_agents: '',
})
const runtime = ref<PlaybackRuntime | null>(null)

/** 下载与设备风控：这批 SystemConfig 键的唯一写入口（系统设置页只读回显） */
const ops = ref({ allow_download: 'true', device_limit_per_user: '0', device_limit_auto_evict: 'false', login_log_retention_days: '' })
/** 下载与设备配置没读到：表单里不是生效值，禁止保存（否则会用空值覆盖线上配置） */
const opsError = ref(false)

/** CDN 域名预留（播放三层第 2/3 层）：只做域名预留，默认关闭 */
const cdnConfig = ref<CdnConfig>({
  domain: '', normalized: '', enabled: false, segment_cache_header: '',
})
/** 服务端注册的线路清单（relay / cdn / cache）——展示用，不在这里改 */
const cdnPlayLines = ref<string[]>([])

/**
 * VPS 本地缓存（播放线路「本地缓存」）：默认关闭，开启后热门片自动拉到本机。
 *
 * 用 `reactive` 而不是 `ref`：这一整个对象都是表单字段，模板里到处是 `localCache.xxx` 的
 * v-model 与回显，`reactive` 的对象语义最直白，也不依赖 ref 在模板里的解包行为。
 */
const localCache = reactive<LocalCacheConfig>({
  enabled: false, dir: '', default_dir: '', max_gb: 500, max_bytes: 0,
  hot_days: 7, hot_plays: 3, rate_mbps: 20, busy_rate_mbps: 2, play_line: 'cache',
  max_attempts: 3, active_playback_window_sec: 300,
})
const cacheStats = ref<LocalCacheStats | null>(null)
const cacheEntries = ref<LocalCacheEntryInfo[]>([])

// fmtBytes 用 composable 里的那份（与危险操作文案同一个函数，避免两处实现漂移）
function fmtRate(r: number | null): string {
  return r === null || r === undefined ? '—' : `${(r * 100).toFixed(1)}%`
}

const cacheStateLabel: Record<string, string> = {
  pending: '排队中', downloading: '下载中', ready: '已缓存', failed: '已失败',
}

const PLAYBACK_NODE_LABEL: Record<string, string> = {
  ea: '分离部署的 EA 节点',
  external: '已有 Emby 服',
  panel: '面板自己（一体化）',
}

async function load() {
  loading.value = true
  loadError.value = false
  try {
    const [p, s, c, lc, ln] = await Promise.all([
      fetchPlaybackPolicy(),
      fetchEconomySettings().catch(() => null),
      fetchCdnConfig().catch(() => null),
      fetchLocalCacheConfig().catch(() => null),
      // 线路可观测读不到不影响本页其它卡片（CDN / 本地缓存配置）
      fetchPlayLines().catch(() => null),
    ])
    policy.value = p.policy
    runtime.value = p.runtime
    playLines.value = ln
    if (c) {
      cdnConfig.value = c.cdn
      cdnPlayLines.value = c.play_lines
    }
    if (lc) {
      // Object.assign 而不是整体替换：reactive 对象保持同一个引用，模板绑定不会断
      Object.assign(localCache, lc.local_cache)
      cacheStats.value = lc.stats
      cacheEntries.value = lc.entries
    }
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
// 四条线路各一张卡片。这里**不改任何线路配置**（配置在下面 CDN / 本地缓存卡片里），
// 只回答“这会儿每条线路怎么样”：能不能用、是不是在降级、有多少人多少流量、效果如何。

const playLines = ref<PlayLinesSnapshot | null>(null)

/** 本机文件整文件直发 / 转码拉流不走本服务响应体，流量口径不包含它们 */
const lineBytesHint = '流量 = 本进程经手的出流量（不含转码时 ffmpeg 的拉流与整文件直发）'

async function savePolicy() {
  savingPolicy.value = true
  try {
    const res = await updatePlaybackPolicy(policy.value)
    policy.value = res.policy
    ElMessage.success('播放策略已保存（出流的节点下次判定即生效）')
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '保存失败')
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
    ElMessage.error(e instanceof Error ? e.message : '保存失败')
  } finally {
    savingOps.value = false
  }
}

async function saveCdn() {
  savingCdn.value = true
  try {
    const res = await updateCdnConfig({
      domain: cdnConfig.value.domain,
      enabled: cdnConfig.value.enabled,
    })
    cdnConfig.value = res.cdn
    ElMessage.success(cdnConfig.value.enabled
      ? 'CDN 预留已启用：播放 URL 走该域名，热门分片由边缘缓存'
      : 'CDN 预留已保存（未启用，播放 URL 与升级前逐字节一致）')
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '保存失败')
  } finally {
    savingCdn.value = false
  }
}

const blockedCount = computed(
  () => (policy.value.blocked_agents || '').split(/[,，\n]/).filter((v) => v.trim()).length,
)
const idleSeconds = computed(() => Math.round(runtime.value?.idle_timeout_seconds ?? 0))

/**
 * 数字框写回兜底：el-input-number 自身会钳制，这里再做一次「取整 + 范围」写回，
 * 保证提交的永远是合法整数；清空 / 非法输入回落到下限，不会把 undefined 留在表单里。
 */
function clampCacheNumber(
  field: 'max_gb' | 'hot_days' | 'hot_plays' | 'rate_mbps',
  lo: number,
  hi: number,
  raw: unknown,
) {
  const num = Math.trunc(Number(raw))
  localCache[field] = Number.isFinite(num) ? Math.min(hi, Math.max(lo, num)) : lo
}

async function saveLocalCache() {
  savingCache.value = true
  try {
    const res = await updateLocalCacheConfig({
      enabled: localCache.enabled,
      dir: localCache.dir,
      max_gb: Number(localCache.max_gb) || 0,
      hot_days: Number(localCache.hot_days) || 1,
      hot_plays: Number(localCache.hot_plays) || 1,
      rate_mbps: Number(localCache.rate_mbps) || 0,
    })
    Object.assign(localCache, res.local_cache)
    cacheStats.value = res.stats
    ElMessage.success(localCache.enabled
      ? '本地缓存已启用：热门片会限速拉到本机，用户侧新增「本地缓存」线路'
      : '本地缓存配置已保存（未启用，播放行为与升级前一致）')
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '保存失败')
  } finally {
    savingCache.value = false
  }
}

async function cleanLocalCacheMode(mode: 'ready' | 'all') {
  // 危险操作（清空本地缓存）的确认与执行在 useDangerOps——与「系统设置 → 危险操作」同一份实现，
  // 这里只负责清完之后刷新本页的缓存读数
  cleaningCache.value = true
  try {
    const ok = await dangerOps.cleanCache(mode)
    if (!ok) return
    cacheStats.value = dangerOps.cacheStats.value
    const fresh = await fetchLocalCacheConfig().catch(() => null)
    if (fresh) {
      cacheEntries.value = fresh.entries
      cacheStats.value = fresh.stats
    }
  } finally {
    cleaningCache.value = false
  }
}
</script>

<template>
  <div class="admin-page client-policy">
    <PageHeader
      eyebrow="安全与准入"
      title="客户端策略"
      description="转码、客户端准入、下载与设备、播放线路：改完对面板与出流的节点同时生效（管理员不受这些策略限制）。"
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

    <!-- 运行态：改上限之前先知道现在跑到什么程度 -->
    <SectionCard
      v-if="runtime"
      title="运行态"
      :icon="Gauge"
      meta="本进程"
      description="分离部署时转码跑在 EA 上，这里的数字是面板本机的。"
    >
      <div class="tile-grid">
        <StatTile
          label="正在转码"
          :icon="Cpu"
          :value="runtime.active_transcodes"
          :tone="runtime.active_transcodes > 0 ? 'warn' : 'plain'"
        />
        <StatTile
          :label="policy.max_concurrent_transcodes ? '策略上限' : '内置上限'"
          :icon="Target"
          :value="policy.max_concurrent_transcodes || runtime.capacity"
          suffix="路"
        />
        <StatTile label="闲置回收" :icon="Clock" :value="idleSeconds" suffix="s" />
        <StatTile
          label="ffmpeg"
          :icon="Tv"
          :value="runtime.ffmpeg_available ? '可用' : '未安装'"
          :tone="runtime.ffmpeg_available ? 'ok' : 'warn'"
          class="text-tile"
        />
        <StatTile
          label="当前出流"
          :icon="Server"
          :value="PLAYBACK_NODE_LABEL[runtime.playback_node] || runtime.playback_node"
          class="text-tile"
        />
      </div>
      <template #footer>
        内置上限 {{ runtime.capacity }} 路（来自 <code>EMBY_MAX_TRANSCODES</code> 或 CPU 核数）；
        超上限时只拒绝<strong>新的</strong>转码请求，不会中断正在看的人。
      </template>
    </SectionCard>

    <!-- 转码与清晰度 -->
    <SectionCard title="转码与清晰度" :icon="Tv" description="控制服务端转码：省 CPU / 省上行与兼容性之间的取舍。">
      <div class="cp-field">
        <div class="cp-field-main">
          <label class="cp-label" for="cp-transcode">允许服务端转码</label>
          <p class="cp-hint">
            关闭后只放直连 / 直接播放：省 CPU 与上行，但客户端兼容性会变差
            （网页端播放器遇到不支持的编码会播不了）。
          </p>
        </div>
        <el-switch id="cp-transcode" v-model="policy.transcode_enabled" :disabled="!isSuper" />
      </div>

      <div class="cp-field">
        <div class="cp-field-main">
          <label class="cp-label">并发转码上限</label>
          <p class="cp-hint">
            0 = 用进程内置上限（默认按 CPU 核数）。设成具体值后，满了会<strong>拒绝新的转码请求</strong>
            并提示稍后再试，而不是把整台机器的 CPU 打满、所有人一起卡。
          </p>
        </div>
        <el-input-number
          v-model="policy.max_concurrent_transcodes"
          :min="0" :max="200"
          :disabled="!isSuper"
          controls-position="right"
          class="cp-num"
        />
      </div>

      <template #footer>
        <div class="cp-save">
          <span class="cp-save-hint">与「客户端准入」同属播放策略，保存任一处都会一起提交。</span>
          <el-button type="primary" :loading="savingPolicy" :disabled="!isSuper" @click="savePolicy">
            <Save :size="14" class="btn-ico" />保存
          </el-button>
        </div>
      </template>
    </SectionCard>

    <!-- 客户端准入 -->
    <SectionCard title="客户端准入" :icon="ShieldBan" :meta="`${blockedCount} 条黑名单规则`">
      <div class="cp-field is-column">
        <label class="cp-label">客户端黑名单（UA 关键词，逗号分隔）</label>
        <el-input
          v-model="policy.blocked_agents"
          type="textarea"
          :rows="2"
          :disabled="!isSuper"
          placeholder="例如：Old-TV, Emby/2.0, 盗版播放器"
        />
        <p class="cp-hint">
          按子串匹配（不区分大小写）：命中就拒绝，连播放信息都拿不到。
          <strong>管理员不受限</strong>，排障时不会被自己的规则挡住。
          另一处在「安全与准入 → <router-link :to="{ name: 'AccessGuard' }" class="cp-link">访问拦截</router-link>」：
          区别是这里只管<strong>播放准入</strong>，那里是全站访问（连面板登录都会拦）。
        </p>
      </div>

      <div class="cp-field is-column">
        <label class="cp-label">客户端白名单（填了就只放行列表内的客户端）</label>
        <el-input
          v-model="policy.allowed_agents"
          type="textarea"
          :rows="2"
          :disabled="!isSuper"
          placeholder="例如：Emby, Infuse, SenPlayer（留空 = 不限制）"
        />
        <p class="cp-hint">
          白名单优先于黑名单；开启后无法识别 UA 的请求（脚本 / 未知客户端）也会被拒。
        </p>
      </div>

      <template #footer>
        <div class="cp-save">
          <span class="cp-save-hint">与「转码与清晰度」同属播放策略，保存任一处都会一起提交。</span>
          <el-button type="primary" :loading="savingPolicy" :disabled="!isSuper" @click="savePolicy">
            <Save :size="14" class="btn-ico" />保存客户端准入
          </el-button>
        </div>
      </template>
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
      :meta="playLines ? '中转（单路径）' : (loading ? '加载中…' : '')"
      :description="playLines?.scope_note || lineBytesHint"
    >
      <p class="cp-hint cp-lead">
        只有一条播放路径：<strong>中转</strong>（视频经服务器转发）。热门内容自动走本地缓存
        与 CF 边缘缓存，无需用户选择。
      </p>

      <div v-if="playLines" class="line-card">
        <div class="line-head">
          <span class="au-badge au-badge-amber">代理中转</span>
          <p class="line-summary">{{ playLines.summary }}</p>
        </div>
        <div class="tile-grid">
          <StatTile label="播放请求（本进程）" :value="playLines.requests" />
          <StatTile label="出流量（本进程）" :value="fmtBytes(playLines.bytes_out)" />
        </div>
        <ul class="line-ready">
          <li><span class="line-key">CDN</span>{{ playLines.cdn.ready_note }}</li>
          <li>
            <span class="line-key">本地缓存</span>{{ playLines.cache.ready_note }}
            <template v-if="playLines.cache.hit_rate !== null && playLines.cache.hit_rate !== undefined">
              （命中率 {{ (playLines.cache.hit_rate * 100).toFixed(1) }}%）
            </template>
          </li>
        </ul>
      </div>
      <EmptyState
        v-else-if="!loading"
        compact
        :icon="Activity"
        title="播放路径数据读取失败"
        description="下方策略与配置不受影响；点右上角「刷新」重试。"
      />
    </SectionCard>

    <SectionCard title="CDN 域名预留" :icon="Cloud">
      <template #actions>
        <span class="au-badge" :class="cdnConfig.enabled ? 'au-badge-green' : 'au-badge-muted'">
          {{ cdnConfig.enabled ? '已启用' : '未启用（默认）' }}
        </span>
      </template>

      <p class="cp-hint cp-lead">
        把一个回源到本服务的 CDN 域名填进来并启用：播放 URL（视频流 / HLS 播放列表 / 字幕）
        改走该域名，热门视频分片由 CDN 边缘缓存，省掉源站（Google Drive）的单文件下载配额。
        <strong>只做域名预留</strong>：备案、证书、回源与缓存规则都在 CDN 厂商控制台自行配置，本服务不代管。
      </p>

      <div class="cp-field">
        <div class="cp-field-main">
          <label class="cp-label">CDN 域名</label>
          <p class="cp-hint">
            例如 cdn.example.com 或 https://cdn.example.com（省略协议默认 https）。
            该域名必须回源到本服务，否则播放会失败。
          </p>
        </div>
        <el-input
          v-model="cdnConfig.domain"
          class="cp-input"
          :disabled="!isSuper"
          placeholder="cdn.example.com"
          clearable
        />
      </div>

      <div class="cp-field">
        <div class="cp-field-main">
          <label class="cp-label">启用 CDN（总开关）</label>
          <p class="cp-hint">
            关闭时（默认）播放 URL 与升级前一致；开启后还需域名合法才生效。
            用户侧「线路选择」里的 cdn 线路也只在总开关开启后才出现并生效。
          </p>
        </div>
        <el-switch v-model="cdnConfig.enabled" :disabled="!isSuper" />
      </div>

      <div class="cp-field">
        <div class="cp-field-main">
          <label class="cp-label">缓存口径</label>
          <p class="cp-hint">
            视频分片：<code>{{ cdnConfig.segment_cache_header || 'public, max-age=300, s-maxage=21600' }}</code>
            （边缘可缓存）；播放列表 / API / 302 跳转：<code>no-store</code>（绝不被缓存）。
          </p>
        </div>
        <span class="cp-value">
          {{ cdnConfig.normalized || '域名未填写' }}
          <template v-if="cdnPlayLines.length">· 线路 {{ cdnPlayLines.join(' / ') }}</template>
        </span>
      </div>

      <template #footer>
        <div class="cp-save">
          <span class="cp-save-hint">保存后新的播放 URL 立即按此生成。</span>
          <el-button type="primary" :loading="savingCdn" :disabled="!isSuper" @click="saveCdn">
            <Save :size="14" class="btn-ico" />保存 CDN 预留
          </el-button>
        </div>
      </template>
    </SectionCard>

    <!-- VPS 本地缓存（播放线路「本地缓存」，默认关闭） -->
    <SectionCard title="VPS 本地缓存" :icon="HardDrive">
      <template #actions>
        <span class="au-badge" :class="localCache.enabled ? 'au-badge-green' : 'au-badge-muted'">
          {{ localCache.enabled ? '已启用' : '未启用（默认）' }}
        </span>
      </template>

      <p class="cp-hint cp-lead">
        把热门的远程挂载片提前拉到 VPS 本机磁盘：用户切到「本地缓存」线路时优先读本机，
        没有才回源并触发缓存。下载<strong>单线程 + 限速</strong>，有人在播放时自动降到
        {{ localCache.busy_rate_mbps }}MB/s 让路，不碰 cdn / relay 两条现有线路；
        超配额按 LRU 删最久未访问的副本。只缓存远程挂载来源的条目（本机文件不需要副本）。
      </p>

      <template v-if="cacheStats">
        <div class="tile-grid">
          <StatTile
            label="已占用"
            :icon="Database"
            :value="fmtBytes(cacheStats.bytes_used)"
            :suffix="`/ ${cacheStats.max_bytes ? fmtBytes(cacheStats.max_bytes) : '不限'}`"
          />
          <StatTile
            label="命中率"
            :icon="Target"
            :value="fmtRate(cacheStats.hit_rate)"
            :hint="`命中 ${cacheStats.hits} / 未命中 ${cacheStats.misses}`"
          />
          <StatTile
            label="已缓存"
            :icon="HardDrive"
            :value="cacheStats.entries.ready || 0"
            :suffix="`/ 共 ${cacheStats.entries_total} 条`"
          />
          <StatTile
            label="磁盘剩余"
            :icon="Database"
            :value="cacheStats.dir_exists ? fmtBytes(cacheStats.disk_free_bytes) : '目录不存在'"
            :tone="cacheStats.dir_exists ? 'plain' : 'warn'"
          />
          <StatTile
            label="下载中 / 排队"
            :icon="Download"
            :value="`${cacheStats.entries.downloading || 0} / ${cacheStats.entries.pending || 0}`"
          />
        </div>
        <p class="cp-hint cp-stats-foot">
          目录 <code>{{ cacheStats.dir }}</code>；热门规则：近 {{ localCache.hot_days }} 天播放
          ≥ {{ localCache.hot_plays }} 次即自动缓存；下载限速
          {{ localCache.rate_mbps ? `${localCache.rate_mbps} MB/s` : '不限速' }}。
          <template v-if="cacheStats.entries.failed">失败 {{ cacheStats.entries.failed }} 条（可在下方清理）。</template>
        </p>
      </template>

      <div class="cp-field">
        <div class="cp-field-main">
          <label class="cp-label">启用本地缓存（总开关）</label>
          <p class="cp-hint">
            关闭时（默认）播放行为与升级前一致；开启后用户侧「线路选择」才会出现 cache 线路，
            后台 worker 开始按热门规则拉片。
          </p>
        </div>
        <el-switch v-model="localCache.enabled" :disabled="!isSuper" />
      </div>

      <div class="cp-field">
        <div class="cp-field-main">
          <label class="cp-label">缓存目录</label>
          <p class="cp-hint">
            绝对路径，留空用默认目录 <code>{{ localCache.default_dir }}</code>。
            目录所在磁盘要装得下配额，建议指向数据盘。
          </p>
        </div>
        <el-input
          v-model="localCache.dir"
          class="cp-input"
          :disabled="!isSuper"
          :placeholder="localCache.default_dir"
          clearable
        />
      </div>

      <div class="cp-field">
        <div class="cp-field-main">
          <label class="cp-label">最大占用（GB）</label>
          <p class="cp-hint">超过配额时按 LRU 删除最久未访问的副本；0 = 不限制。</p>
        </div>
        <el-input-number
          v-model="localCache.max_gb"
          :min="0" :max="100000" :step="50"
          :disabled="!isSuper"
          controls-position="right"
          class="cp-num"
          @change="clampCacheNumber('max_gb', 0, 100000, $event)"
        />
      </div>

      <div class="cp-field">
        <div class="cp-field-main">
          <label class="cp-label">热门判定规则</label>
          <p class="cp-hint">近 N 天内播放达到 M 次的片子自动入队下载（按播放次数从多到少排队）。</p>
        </div>
        <!--
          两个数字框放在 .num-pair 里：桌面并排；窄屏（响应式层会把 .el-input-number 拉成 100%）
          允许换行、并保留可读宽度——之前挤在一行里时数字会被两侧按钮盖住、看着像空框。
        -->
        <div class="num-pair">
          <el-input-number
            v-model="localCache.hot_days"
            :min="1" :max="90" :step="1"
            :disabled="!isSuper"
            controls-position="right"
            @change="clampCacheNumber('hot_days', 1, 90, $event)"
          />
          <span class="cp-unit">天内 ≥</span>
          <el-input-number
            v-model="localCache.hot_plays"
            :min="1" :max="1000" :step="1"
            :disabled="!isSuper"
            controls-position="right"
            @change="clampCacheNumber('hot_plays', 1, 1000, $event)"
          />
          <span class="cp-unit">次</span>
        </div>
      </div>

      <div class="cp-field">
        <div class="cp-field-main">
          <label class="cp-label">下载限速（MB/s）</label>
          <p class="cp-hint">
            缓存下载占用的带宽上限，0 = 不限速；检测到有人在播放时自动降到
            {{ localCache.busy_rate_mbps }}MB/s 给播放让路，绝不抢当前播放的带宽。
          </p>
        </div>
        <el-input-number
          v-model="localCache.rate_mbps"
          :min="0" :max="10000" :step="5"
          :disabled="!isSuper"
          controls-position="right"
          class="cp-num"
          @change="clampCacheNumber('rate_mbps', 0, 10000, $event)"
        />
      </div>

      <div v-if="cacheEntries.length" class="cache-list">
        <div class="cache-list-head">最近条目（前 6 条）</div>
        <div v-for="e in cacheEntries.slice(0, 6)" :key="e.item_guid" class="cache-item">
          <span class="ci-name">{{ e.name || e.source_path }}</span>
          <span class="ci-meta">
            <span
              class="au-badge"
              :class="e.state === 'failed' ? 'au-badge-rose' : e.state === 'ready' ? 'au-badge-green' : 'au-badge-muted'"
            >{{ cacheStateLabel[e.state] || e.state }}</span>
            {{ fmtBytes(e.file_size) }} · 命中 {{ e.hits }}
            <template v-if="e.last_accessed_at"> · 最近访问 {{ e.last_accessed_at.slice(0, 16).replace('T', ' ') }}</template>
          </span>
        </div>
      </div>

      <template #footer>
        <div class="cp-save cp-save-split">
          <div class="cp-save-left">
            <el-button
              type="danger" plain
              :loading="cleaningCache" :disabled="!isSuper || !cacheEntries.length"
              @click="cleanLocalCacheMode('ready')"
            >
              <Trash2 :size="14" class="btn-ico" />清理缓存副本
            </el-button>
            <el-button
              type="danger" plain
              :loading="cleaningCache" :disabled="!isSuper"
              @click="cleanLocalCacheMode('all')"
            >
              <Trash2 :size="14" class="btn-ico" />清空全部
            </el-button>
            <RouterLink class="danger-jump" :to="{ name: 'Settings', query: { tab: 'danger', op: 'cache' } }">
              危险操作中心 →
            </RouterLink>
          </div>
          <el-button type="primary" :loading="savingCache" :disabled="!isSuper" @click="saveLocalCache">
            <Save :size="14" class="btn-ico" />保存本地缓存
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
        这里只放「客户端能做什么」（转码、清晰度、准入、下载与设备）；
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
