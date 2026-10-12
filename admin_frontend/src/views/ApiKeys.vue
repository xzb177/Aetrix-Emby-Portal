<script setup lang="ts">
/**
 * API Key 管理（Service Token，v2.58.0）
 *
 * - 第三方系统用长期有效的 API Key 调用受保护接口（X-API-Key 请求头）
 * - 明文只在创建时返回一次：大弹窗展示，红色提示立即复制
 * - 列表只展示 key_id 掩码，绝不返回明文
 * - 品牌约束：只用 --au-* 变量，零新增色值；暗房影院深色主题
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { KeyRound, Plus, RefreshCw, Copy, Trash2, Pencil } from 'lucide-vue-next'
import { EmptyState, PageHeader } from '@/components/ui'
import {
  createApiKey,
  fetchApiKeys,
  revokeApiKey,
  updateApiKey,
  type ApiKeyRow,
  type ApiKeyCreateResponse,
} from '@/api/admin'

const keys = ref<ApiKeyRow[]>([])
const loading = ref(false)
const loadError = ref('')

// 创建弹窗
const showCreate = ref(false)
const creating = ref(false)
const form = ref({ name: '', scopes: ['read'] as string[], expires_days: 365 as number | null })
const noExpire = ref(false)

// 明文展示弹窗（只显示一次）
const showPlaintext = ref(false)
const plaintextKey = ref('')
const plaintextName = ref('')

// 编辑弹窗
const showEdit = ref(false)
const editing = ref<ApiKeyRow | null>(null)
const editForm = ref({ name: '', scopes: [] as string[], is_active: true })

const SCOPES = [
  { value: 'read', label: '只读', desc: '读数据、查状态' },
  { value: 'write', label: '读写', desc: '包含只读，可写数据' },
  { value: 'admin', label: '管理', desc: '包含读写，全部权限' },
]

const activeCount = computed(() => keys.value.filter((k) => k.is_active).length)

function scopeLabel(s: string) {
  return SCOPES.find((x) => x.value === s)?.label || s
}

function scopeClass(s: string) {
  if (s === 'admin') return 'is-down'
  if (s === 'write') return 'is-warn'
  return 'is-ok'
}

function fmtTime(t: string | null) {
  if (!t) return '—'
  try {
    return new Date(t).toLocaleString('zh-CN', { hour12: false })
  } catch {
    return t
  }
}

function isExpired(k: ApiKeyRow) {
  if (!k.expires_at) return false
  return new Date(k.expires_at).getTime() < Date.now()
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const r = await fetchApiKeys()
    keys.value = r.items || []
  } catch (e: any) {
    loadError.value = e?.message || '加载失败'
  } finally {
    loading.value = false
  }
}

function openCreate() {
  form.value = { name: '', scopes: ['read'], expires_days: 365 }
  noExpire.value = false
  showCreate.value = true
}

async function doCreate() {
  if (!form.value.name.trim()) {
    ElMessage.warning('请填写名称，比如"XX 系统对接"')
    return
  }
  if (!form.value.scopes.length) {
    ElMessage.warning('至少选一个权限范围')
    return
  }
  creating.value = true
  try {
    const r: ApiKeyCreateResponse = await createApiKey({
      name: form.value.name.trim(),
      scopes: form.value.scopes,
      expires_days: noExpire.value ? null : form.value.expires_days,
    })
    showCreate.value = false
    plaintextKey.value = r.key
    plaintextName.value = r.name
    showPlaintext.value = true
    await load()
  } catch (e: any) {
    ElMessage.error(e?.message || '创建失败')
  } finally {
    creating.value = false
  }
}

async function copyPlaintext() {
  try {
    await navigator.clipboard.writeText(plaintextKey.value)
    ElMessage.success('已复制到剪贴板')
  } catch {
    ElMessage.warning('复制失败，请手动选中复制')
  }
}

function closePlaintext() {
  showPlaintext.value = false
  plaintextKey.value = ''
  ElMessage.info('明文已关闭，之后无法再查看')
}

function openEdit(k: ApiKeyRow) {
  editing.value = k
  editForm.value = { name: k.name, scopes: [...k.scopes], is_active: k.is_active }
  showEdit.value = true
}

async function doEdit() {
  if (!editing.value) return
  try {
    await updateApiKey(editing.value.key_id, {
      name: editForm.value.name,
      scopes: editForm.value.scopes,
      is_active: editForm.value.is_active,
    })
    ElMessage.success('已保存')
    showEdit.value = false
    await load()
  } catch (e: any) {
    ElMessage.error(e?.message || '保存失败')
  }
}

async function doRevoke(k: ApiKeyRow) {
  try {
    await ElMessageBox.confirm(
      `确定吊销「${k.name}」（${k.key_id_masked}）吗？吊销后使用该 Key 的第三方将立即无法访问，不可恢复。`,
      '吊销 API Key',
      { confirmButtonText: '确认吊销', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await revokeApiKey(k.key_id)
    ElMessage.success('已吊销')
    await load()
  } catch (e: any) {
    ElMessage.error(e?.message || '吊销失败')
  }
}

onMounted(load)
</script>

<template>
  <div class="apikey-page">
    <PageHeader title="API Key 管理" desc="第三方系统的长期访问凭证（Service Token），用 X-API-Key 请求头鉴权">
      <template #actions>
        <el-button :icon="RefreshCw" @click="load" :loading="loading">刷新</el-button>
        <el-button type="primary" :icon="Plus" @click="openCreate">创建 Key</el-button>
      </template>
    </PageHeader>

    <div class="summary-bar" v-if="!loading && !loadError">
      <span class="summary-item"><b>{{ keys.length }}</b> 个 Key</span>
      <span class="summary-item"><b class="is-ok">{{ activeCount }}</b> 生效中</span>
      <span class="summary-item"><b class="is-down">{{ keys.length - activeCount }}</b> 已吊销/停用</span>
    </div>

    <el-skeleton :rows="6" animated v-if="loading" />
    <EmptyState v-else-if="loadError" title="加载失败" :description="loadError" @retry="load" />
    <EmptyState v-else-if="!keys.length" title="还没有 API Key" description="点右上角「创建 Key」" />

    <div class="key-grid" v-else>
      <div v-for="k in keys" :key="k.id" class="key-card" :class="{ 'is-off': !k.is_active || isExpired(k) }">
        <div class="key-head">
          <span class="key-name">{{ k.name }}</span>
          <el-tag :type="k.is_active && !isExpired(k) ? 'success' : 'danger'" size="small">
            {{ !k.is_active ? '已吊销' : isExpired(k) ? '已过期' : '生效中' }}
          </el-tag>
        </div>
        <div class="key-id"><KeyRound class="ic" />{{ k.key_id_masked }}</div>
        <div class="key-scopes">
          <span v-for="s in k.scopes" :key="s" class="scope-pill" :class="scopeClass(s)">{{ scopeLabel(s) }}</span>
        </div>
        <div class="key-meta">
          <span>过期：{{ k.expires_at ? fmtTime(k.expires_at) : '永不过期' }}</span>
          <span>最后使用：{{ fmtTime(k.last_used_at) }}</span>
        </div>
        <div class="key-actions">
          <el-button size="small" :icon="Pencil" @click="openEdit(k)">编辑</el-button>
          <el-button v-if="k.is_active" size="small" type="danger" :icon="Trash2" @click="doRevoke(k)">吊销</el-button>
        </div>
      </div>
    </div>

    <div class="usage-hint">
      <b>第三方怎么用：</b>请求头带 <code>X-API-Key: ak_...</code> 即可。
      先调 <code>GET /api/admin/api-keys/verify</code> 验证连通性。
      权限层级：管理 &gt; 读写 &gt; 只读。
    </div>

    <!-- 创建弹窗 -->
    <el-dialog v-model="showCreate" title="创建 API Key" width="480px">
      <el-form label-width="90px">
        <el-form-item label="名称">
          <el-input v-model="form.name" placeholder="比如：XX 系统对接" maxlength="100" />
        </el-form-item>
        <el-form-item label="权限范围">
          <el-checkbox-group v-model="form.scopes">
            <el-checkbox v-for="s in SCOPES" :key="s.value" :label="s.value">
              {{ s.label }}<span class="scope-desc">（{{ s.desc }}）</span>
            </el-checkbox>
          </el-checkbox-group>
        </el-form-item>
        <el-form-item label="有效期">
          <el-checkbox v-model="noExpire">永不过期</el-checkbox>
          <el-input-number v-if="!noExpire" v-model="form.expires_days" :min="1" :max="3650" style="margin-left: 12px" />
          <span v-if="!noExpire" class="scope-desc"> 天</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showCreate = false">取消</el-button>
        <el-button type="primary" @click="doCreate" :loading="creating">创建</el-button>
      </template>
    </el-dialog>

    <!-- 明文展示（只一次） -->
    <el-dialog v-model="showPlaintext" title="创建成功" width="520px" :close-on-click-modal="false" @close="closePlaintext">
      <el-alert type="error" :closable="false" show-icon>
        <b>只显示一次！</b>请立即复制保存，关闭后无法再查看。Key 明文不会存进数据库。
      </el-alert>
      <div class="plaintext-box">
        <div class="plaintext-name">{{ plaintextName }}</div>
        <code class="plaintext-key">{{ plaintextKey }}</code>
        <el-button :icon="Copy" @click="copyPlaintext" type="primary" plain>复制</el-button>
      </div>
    </el-dialog>

    <!-- 编辑弹窗 -->
    <el-dialog v-model="showEdit" title="编辑 API Key" width="480px">
      <el-form label-width="90px">
        <el-form-item label="名称">
          <el-input v-model="editForm.name" maxlength="100" />
        </el-form-item>
        <el-form-item label="权限范围">
          <el-checkbox-group v-model="editForm.scopes">
            <el-checkbox v-for="s in SCOPES" :key="s.value" :label="s.value">{{ s.label }}</el-checkbox>
          </el-checkbox-group>
        </el-form-item>
        <el-form-item label="状态">
          <el-switch v-model="editForm.is_active" active-text="生效" inactive-text="停用" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showEdit = false">取消</el-button>
        <el-button type="primary" @click="doEdit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.apikey-page {
  padding: 20px;
  max-width: 1200px;
  margin: 0 auto;
}
.summary-bar {
  display: flex;
  gap: 24px;
  padding: 12px 16px;
  margin-bottom: 16px;
  background: var(--au-surface);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-lg);
  color: var(--au-text-2);
  font-size: 14px;
}
.summary-item b {
  font-size: 18px;
  margin-right: 4px;
  color: var(--au-text);
}
.is-ok { color: var(--au-success); }
.is-warn { color: var(--au-warning); }
.is-down { color: var(--au-danger); }
.key-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 16px;
}
.key-card {
  background: var(--au-surface);
  border: 1px solid var(--au-border);
  border-left: 3px solid var(--au-success);
  border-radius: var(--au-r-lg);
  padding: 16px;
}
.key-card.is-off {
  border-left-color: var(--au-danger);
  opacity: 0.75;
}
.key-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}
.key-name {
  font-weight: 600;
  font-size: 15px;
  color: var(--au-text);
}
.key-id {
  display: flex;
  align-items: center;
  gap: 6px;
  font-family: monospace;
  color: var(--au-text-2);
  margin-bottom: 8px;
}
.key-id .ic { width: 14px; height: 14px; }
.key-scopes {
  display: flex;
  gap: 8px;
  margin-bottom: 8px;
}
.scope-pill {
  font-size: 12px;
  padding: 2px 10px;
  border-radius: 999px;
  border: 1px solid currentColor;
}
.key-meta {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
  color: var(--au-text-2);
  margin-bottom: 12px;
}
.key-actions {
  display: flex;
  gap: 8px;
}
.usage-hint {
  margin-top: 20px;
  padding: 12px 16px;
  background: var(--au-surface);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-lg);
  font-size: 13px;
  color: var(--au-text-2);
  line-height: 1.8;
}
.usage-hint code {
  background: var(--au-bg);
  padding: 2px 6px;
  border-radius: 4px;
  font-family: monospace;
}
.scope-desc {
  color: var(--au-text-2);
  font-size: 12px;
}
.plaintext-box {
  margin-top: 16px;
  padding: 16px;
  background: var(--au-bg);
  border: 1px dashed var(--au-danger);
  border-radius: var(--au-r-lg);
  text-align: center;
}
.plaintext-name {
  font-weight: 600;
  margin-bottom: 8px;
  color: var(--au-text);
}
.plaintext-key {
  display: block;
  font-family: monospace;
  font-size: 14px;
  word-break: break-all;
  padding: 12px;
  margin-bottom: 12px;
  background: var(--au-surface);
  border-radius: 8px;
  color: var(--au-text);
  user-select: all;
}
</style>
