<script setup lang="ts">
/**
 * 播放策略（能力配置）
 *
 * 回答「播放时字节从哪走」：中继（relay，本服务代理转发）或直链
 * （direct，名单内的热门文件 302 到服主自有对象存储，字节不经本机）。
 * 只做能力：用哪种由服主决定，默认 relay 与升级前行为完全一致。
 *
 * 直链源是 S3 兼容存储（R2 / B2 / Bunny 等）：服主把热门文件的副本按
 * 对象键模板放进桶里、把 file_id 登记进直链名单，播放时才发签名直链；
 * 名单外、源不可用、配置不全一律自动回落中继，不会打断播放。
 */
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Play, Plus, RefreshCw, Save, Server, Trash2 } from 'lucide-vue-next'
import { EmptyState, PageHeader, SectionCard } from '@/components/ui'
import {
  fetchPlaybackConfig,
  savePlaybackConfig,
  type PlaybackConfig,
  type PlaybackDirectSource,
} from '@/api/admin'
import { toAdminError } from '@/utils/errorMessage'

const loading = ref(false)
const loadError = ref('')
const saving = ref(false)

const strategy = ref('relay')
const urlTtlSec = ref(3600)
const keyTemplate = ref('{file_id}')
const sources = ref<PlaybackDirectSource[]>([])
const hotlistText = ref('')

function applyConfig(cfg: PlaybackConfig) {
  strategy.value = cfg.strategy || 'relay'
  urlTtlSec.value = cfg.url_ttl_sec || 3600
  keyTemplate.value = cfg.key_template || '{file_id}'
  sources.value = (cfg.sources || []).map((s) => ({ ...s }))
  hotlistText.value = (cfg.hotlist || []).join('\n')
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const resp = await fetchPlaybackConfig()
    if (resp?.config) applyConfig(resp.config)
  } catch (e) {
    loadError.value = toAdminError(e)
  } finally {
    loading.value = false
  }
}

function addSource() {
  sources.value.push({
    name: '',
    type: 's3',
    endpoint: '',
    bucket: '',
    region: '',
    access_key_id: '',
    secret_key: '',
    has_secret_key: false,
    path_style: true,
    enabled: true,
  })
}

function removeSource(idx: number) {
  sources.value.splice(idx, 1)
}

async function save() {
  saving.value = true
  try {
    const hotlist = hotlistText.value
      .split('\n')
      .map((x) => x.trim())
      .filter(Boolean)
    const resp = await savePlaybackConfig({
      strategy: strategy.value,
      sources: sources.value,
      hotlist,
      key_template: keyTemplate.value,
      url_ttl_sec: urlTtlSec.value,
    })
    if (resp?.config) applyConfig(resp.config)
    ElMessage.success('播放策略已保存，即时生效')
  } catch (e) {
    ElMessage.error(toAdminError(e))
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="pb-page">
    <PageHeader
      eyebrow="媒体服务"
      title="播放策略"
      description="播放字节从哪走：中继经本服务转发，或让名单内的热门文件直连你自己的对象存储。默认中继，与升级前完全一致；改完即时生效。"
    >
      <template #actions>
        <el-button :loading="loading" @click="load">
          <RefreshCw class="pb-ico" />重新读取
        </el-button>
        <el-button type="primary" :loading="saving" @click="save">
          <Save class="pb-ico" />保存
        </el-button>
      </template>
    </PageHeader>

    <EmptyState v-if="loadError" title="读取失败" :description="loadError" />

    <template v-else>
      <SectionCard title="策略" :icon="Play" description="direct 只对直链名单内的文件生效，其余文件与全部异常情况都自动回落中继。">
        <div class="pb-row">
          <span class="pb-label">播放策略</span>
          <el-radio-group v-model="strategy">
            <el-radio-button value="relay">中继（默认）</el-radio-button>
            <el-radio-button value="direct">对象存储直链</el-radio-button>
          </el-radio-group>
        </div>
        <div class="pb-row">
          <span class="pb-label">签名有效期（秒）</span>
          <el-input-number v-model="urlTtlSec" :min="60" :max="86400" :step="60" />
        </div>
        <div class="pb-row">
          <span class="pb-label">对象键模板</span>
          <el-input v-model="keyTemplate" class="pb-wide" placeholder="{file_id}" />
          <span class="pb-hint">副本在桶里的对象键，{file_id} 会替换成文件的 Drive 编号</span>
        </div>
      </SectionCard>

      <SectionCard title="直链源" :icon="Server" description="S3 兼容存储（Cloudflare R2 / Backblaze B2 / Bunny 等）。按从上到下顺序取第一个启用的源。">
        <div v-for="(src, idx) in sources" :key="idx" class="pb-source">
          <div class="pb-source-head">
            <el-input v-model="src.name" class="pb-name" placeholder="源名称，如 r2-hot" />
            <el-switch v-model="src.enabled" active-text="启用" inactive-text="停用" />
            <el-button text type="danger" @click="removeSource(idx)">
              <Trash2 class="pb-ico" />删除
            </el-button>
          </div>
          <div class="pb-grid">
            <label>Endpoint<el-input v-model="src.endpoint" placeholder="https://<account>.r2.cloudflarestorage.com" /></label>
            <label>Bucket<el-input v-model="src.bucket" placeholder="hot" /></label>
            <label>Region<el-input v-model="src.region" placeholder="auto（R2）/ us-west-004（B2）" /></label>
            <label>Access Key ID<el-input v-model="src.access_key_id" /></label>
            <label>Secret Key
              <el-input
                v-model="src.secret_key"
                type="password"
                show-password
                :placeholder="src.has_secret_key ? '已保存，留空表示不改' : '未设置'"
              />
            </label>
            <label class="pb-check">
              寻址方式
              <el-switch v-model="src.path_style" active-text="路径式" inactive-text="虚拟主机式" />
            </label>
          </div>
        </div>
        <el-button @click="addSource"><Plus class="pb-ico" />添加直链源</el-button>
      </SectionCard>

      <SectionCard title="直链名单" description="已在直链源里备好副本的文件，一行一个 Drive 文件编号（file_id）。名单外的文件永远走中继。">
        <el-input
          v-model="hotlistText"
          type="textarea"
          :rows="8"
          placeholder="一行一个 file_id"
        />
        <p class="pb-hint">共 {{ hotlistText.split('\n').filter((x) => x.trim()).length }} 个文件</p>
      </SectionCard>
    </template>
  </div>
</template>

<style scoped>
.pb-page { display: flex; flex-direction: column; gap: 16px; }
.pb-ico { width: 14px; height: 14px; margin-right: 4px; vertical-align: -2px; }
.pb-row { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }
.pb-label { width: 130px; flex: none; color: var(--au-text-2, #666); }
.pb-wide { max-width: 420px; }
.pb-hint { color: var(--au-text-3, #999); font-size: 12px; }
.pb-source { border: 1px solid var(--au-line, #e5e0d5); border-radius: 10px; padding: 12px; margin-bottom: 12px; }
.pb-source-head { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
.pb-name { max-width: 260px; }
.pb-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; }
.pb-grid label { display: flex; flex-direction: column; gap: 6px; font-size: 13px; color: var(--au-text-2, #666); }
.pb-check { justify-content: flex-start; }
</style>
