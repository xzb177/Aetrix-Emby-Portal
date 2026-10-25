<script setup lang="ts">
/**
 * 115 账号
 *
 * 115 以「存储挂载」（类型选 115）的形式接入媒体库：面板 / EA 用 Cookie 型 Web API
 * 列目录、换直链，不需要转存。
 *
 * - 账号配置档：多账号 + 默认账号 + 启用开关；Cookie 不回传明文，可即时校验
 * - 目录浏览：用某个配置档实测一次列目录，既能确认 Cookie 可用，也能看清直挂的目录结构
 *
 * v2.18.0：分享链接转存 / 下载任务那一整套（标签页、任务表、新建任务弹窗）已下线，
 * 页面只保留账号配置。
 *
 * v2.54（暗房影院）：PageHeader + StatTile（账号 / 启用 / 校验失败才染警示）+ flush SectionCard 账号表；
 * 空 / 错态用 EmptyState；目录浏览弹窗改成发丝线列表（文件夹图标代替 emoji、面包屑是可聚焦按钮）。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  AlertTriangle, ChevronRight, Cloud, CircleCheck, Folder, FolderOpen, KeyRound, Plus, RefreshCw, ShieldAlert, ShieldCheck, Trash2,
} from 'lucide-vue-next'
import { EmptyState, PageHeader, SectionCard, StatTile } from '@/components/ui'
import {
  browsePan115,
  createPan115Account,
  deletePan115Account,
  fetchPan115Accounts,
  updatePan115Account,
  verifyPan115Account,
  verifyPan115Cookie,
} from '@/api/admin'
import type { Pan115Account, Pan115DirEntry } from '@/types'
import DataTable from '@/components/DataTable.vue'
import type { DataColumn } from '@/components/DataTable.vue'

const loading = ref(false)
const accounts = ref<Pan115Account[]>([])
const envCookieConfigured = ref(false)
/** 账号列表加载失败（区别于「还没有账号」） */
const loadError = ref(false)

/** 顶部概况：只有「最近校验失败」的账号才值得染色提醒 */
const accountStats = computed(() => ({
  total: accounts.value.length,
  enabled: accounts.value.filter((a) => a.is_enabled).length,
  failed: accounts.value.filter((a) => a.last_verified_at && !a.last_verify_ok).length,
  defaultName: accounts.value.find((a) => a.is_default)?.name || '',
}))

const accountColumns: DataColumn[] = [
  { key: 'name', label: '名称', minWidth: 150, mobile: 'title' },
  { key: 'cookie_preview', label: 'Cookie', width: 130, mobile: 'hide' },
  { key: 'is_default', label: '默认', width: 80 },
  { key: 'is_enabled', label: '状态', width: 100 },
  { key: 'last_verified_at', label: '最近校验', minWidth: 190 },
  { key: 'remark', label: '备注', minWidth: 120, mobile: 'hide' },
  // 校验 / 浏览 / 编辑 / 删除收进「管理」弹窗：行里只留一个入口
  { key: 'actions', label: '操作', width: 100, fixed: 'right', align: 'right' },
]

async function load() {
  loading.value = true
  loadError.value = false
  try {
    const res = await fetchPan115Accounts()
    accounts.value = res.accounts
    envCookieConfigured.value = res.env_cookie_configured
    uaPresets.value = res.ua_presets || []
  } catch {
    // 错误提示由 HTTP 拦截器统一处理；这里只记下失败，给出重试入口
    loadError.value = true
  } finally {
    loading.value = false
  }
}

onMounted(load)

function fmtDate(s: string | null): string {
  if (!s) return '—'
  return s.slice(0, 16).replace('T', ' ')
}

// ==================== 账号配置档 ====================

const accVisible = ref(false)
const accEditing = ref<Pan115Account | null>(null)
const accSaving = ref(false)
const accTesting = ref(false)
const accForm = reactive({ name: '', cookie: '', is_default: false, is_enabled: true, remark: '', ua: '' })
/** UA 预置项由后端下发（前端不自己维护一份，免得两边漂移） */
const uaPresets = ref<{ value: string; label: string }[]>([])

function openAccount(a: Pan115Account | null) {
  accEditing.value = a
  accForm.name = a?.name || ''
  accForm.cookie = ''
  accForm.is_default = a?.is_default ?? false
  accForm.is_enabled = a?.is_enabled ?? true
  accForm.remark = a?.remark || ''
  accForm.ua = a?.ua || ''
  accVisible.value = true
}

async function saveAccount() {
  if (!accForm.name.trim()) {
    ElMessage.warning('请填写账号名称')
    return
  }
  if (!accEditing.value && !accForm.cookie.trim()) {
    ElMessage.warning('请粘贴 115 Cookie')
    return
  }
  accSaving.value = true
  try {
    const payload: Record<string, unknown> = {
      name: accForm.name.trim(),
      is_default: accForm.is_default,
      is_enabled: accForm.is_enabled,
      remark: accForm.remark,
      ua: accForm.ua,
    }
    if (accForm.cookie.trim()) payload.cookie = accForm.cookie.trim()
    if (accEditing.value) {
      await updatePan115Account(accEditing.value.id, payload)
    } else {
      await createPan115Account(payload as { name: string; cookie: string })
    }
    ElMessage.success('已保存')
    accVisible.value = false
    load()
  } finally {
    accSaving.value = false
  }
}

async function testCookie() {
  if (!accForm.cookie.trim() && !accEditing.value) {
    ElMessage.warning('请粘贴 Cookie 后再测试')
    return
  }
  accTesting.value = true
  try {
    if (accForm.cookie.trim()) {
      const res = await verifyPan115Cookie(accForm.cookie.trim())
      ElMessage[res.result.ok ? 'success' : 'error'](
        res.result.ok ? 'Cookie 有效' : res.result.message || 'Cookie 无效'
      )
    } else if (accEditing.value) {
      const res = await verifyPan115Account(accEditing.value.id)
      ElMessage[res.result.ok ? 'success' : 'error'](
        res.result.ok ? 'Cookie 有效' : res.result.message || 'Cookie 无效'
      )
      load()
    }
  } finally {
    accTesting.value = false
  }
}

// ==================== 账号管理弹窗（v2.29.0） ====================
// 以前行尾四个按钮（编辑 / 校验 / 浏览 / 删除）把「这个账号到底能不能用」拆得到处都是：
// 现在一个入口，弹窗里先看「Cookie 有没有、上次校验结果与原因」，再校验 / 浏览 / 改 / 删。
const manage = ref({ visible: false, row: null as Pan115Account | null })

function openManage(row: Pan115Account) {
  manage.value = { visible: true, row }
}

/** 动作后刷新列表，并把弹窗里的账号换成最新快照（校验结果就地可见） */
async function refreshManage(id: number) {
  await load()
  const fresh = accounts.value.find((a) => a.id === id)
  if (fresh) {
    manage.value.row = fresh
  } else {
    manage.value.visible = false
  }
}

/** 从弹窗进编辑 / 浏览：先关掉这一层，避免两个弹窗叠着 */
function editFromManage(row: Pan115Account) {
  manage.value.visible = false
  openAccount(row)
}

function browseFromManage(row: Pan115Account) {
  manage.value.visible = false
  openBrowser(row)
}

async function verify(row: Pan115Account) {
  const res = await verifyPan115Account(row.id)
  ElMessage[res.result.ok ? 'success' : 'error'](
    res.result.ok ? `「${row.name}」Cookie 有效` : res.result.message || 'Cookie 无效'
  )
  await refreshManage(row.id)
}

async function removeAccount(a: Pan115Account) {
  try {
    await ElMessageBox.confirm(
      `删除账号「${a.name}」？绑定了它的媒体库会回退到默认账号。`,
      '确认删除',
      { type: 'warning' }
    )
  } catch {
    return
  }
  await deletePan115Account(a.id)
  ElMessage.success('已删除')
  await refreshManage(a.id)
}

// ==================== 目录浏览（实测一次列目录） ====================

const browseVisible = ref(false)
const browseAccount = ref<Pan115Account | null>(null)
const browseStack = ref<{ cid: string; name: string }[]>([])
const browseDirs = ref<Pan115DirEntry[]>([])
const browseSource = ref('')
const browsing = ref(false)

async function goto(cid: string) {
  browsing.value = true
  try {
    const res = await browsePan115({ cid, account_id: browseAccount.value?.id })
    browseDirs.value = res.entries
    browseSource.value = res.cookie_source
  } catch {
    browseDirs.value = []
  } finally {
    browsing.value = false
  }
}

async function openBrowser(row: Pan115Account) {
  browseAccount.value = row
  browseStack.value = [{ cid: '0', name: '根目录' }]
  browseDirs.value = []
  browseSource.value = ''
  browseVisible.value = true
  await goto('0')
}

function enterDir(entry: Pan115DirEntry) {
  browseStack.value = [...browseStack.value, { cid: entry.cid || entry.fid, name: entry.name }]
  goto(browseStack.value[browseStack.value.length - 1].cid)
}

function popTo(index: number) {
  browseStack.value = browseStack.value.slice(0, index + 1)
  goto(browseStack.value[index].cid)
}

function currentPath(): string {
  return '/' + browseStack.value.slice(1).map((p) => p.name).join('/')
}
</script>

<template>
  <div class="admin-page pan115-page">
    <PageHeader
      eyebrow="媒体"
      title="115 账号"
      description="配置 115 Cookie 以使用「存储来源 → 115」直挂：面板 / EA 直接列目录、换直链播放，不需要转存。"
    >
      <template #actions>
        <el-button :icon="RefreshCw" :loading="loading" @click="load">刷新</el-button>
        <el-button type="primary" :icon="Plus" @click="openAccount(null)">添加账号</el-button>
      </template>
    </PageHeader>

    <section v-if="accounts.length" class="stat-row" aria-label="115 账号概况">
      <StatTile
        label="账号配置档"
        :value="accountStats.total"
        :icon="Cloud"
        :hint="accountStats.defaultName ? `默认：${accountStats.defaultName}` : '还没有默认账号'"
      />
      <StatTile label="启用中" :value="accountStats.enabled" :icon="CircleCheck" />
      <StatTile
        label="最近校验失败"
        :value="accountStats.failed"
        :icon="ShieldAlert"
        :tone="accountStats.failed > 0 ? 'warn' : 'plain'"
        :hint="accountStats.failed > 0 ? 'Cookie 可能过期，点「管理」重新校验' : '没有失效的账号'"
      />
      <StatTile
        label="环境变量兜底"
        :value="envCookieConfigured ? '已配置' : '未配置'"
        :icon="KeyRound"
        hint="PAN115_COOKIE"
      />
    </section>

    <SectionCard
      title="Cookie 配置档"
      :icon="Cloud"
      :meta="accounts.length ? `${accounts.length} 个` : ''"
      :description="`支持多账号与默认账号，媒体库可单独绑定；未绑定 / 未配置时回退到默认账号${envCookieConfigured ? '，环境变量 PAN115_COOKIE 也会作为兜底' : ''}。`"
      flush
    >
      <EmptyState v-if="loadError && !accounts.length" :icon="AlertTriangle" title="账号列表加载失败" description="网络或服务暂时不可用，稍后重试。">
        <template #actions><el-button :loading="loading" @click="load">重试</el-button></template>
      </EmptyState>

      <DataTable
        v-else
        class="flush-table"
        :rows="accounts"
        :columns="accountColumns"
        :loading="loading"
        empty="还没有账号配置档"
        clickable
      >
        <template #cell-cookie_preview="{ row }">
          <span class="mono faint">{{ row.cookie_preview || '—' }}</span>
        </template>

        <template #cell-is_default="{ row }">
          <span v-if="row.is_default" class="au-badge au-badge-amber">默认</span>
          <span v-else class="faint">—</span>
        </template>

        <template #cell-is_enabled="{ row }">
          <span class="au-badge" :class="row.is_enabled ? 'au-badge-green' : 'au-badge-muted'">
            {{ row.is_enabled ? '启用' : '停用' }}
          </span>
        </template>

        <template #cell-last_verified_at="{ row }">
          <template v-if="row.last_verified_at">
            <span class="num">{{ fmtDate(row.last_verified_at) }}</span>
            <span :class="row.last_verify_ok ? 'ok-text' : 'bad-text'">
              {{ row.last_verify_ok ? '有效' : '无效' }}
            </span>
            <span v-if="row.last_verify_message && !row.last_verify_ok" class="sub">
              · {{ row.last_verify_message }}
            </span>
          </template>
          <span v-else class="faint">未校验</span>
        </template>

        <template #cell-remark="{ row }">
          <span v-if="!row.remark" class="faint">—</span>
          <span v-else>{{ row.remark }}</span>
        </template>

        <template #cell-actions="{ row }">
          <!-- 一个入口：校验 / 浏览 / 编辑 / 删除 都在弹窗里（原来这行有 4 个按钮） -->
          <el-button size="small" plain @click="openManage(row)">管理</el-button>
        </template>

        <template #empty>
          <EmptyState
            compact
            :icon="Cloud"
            title="还没有账号配置档"
            :description="envCookieConfigured ? '当前用环境变量 PAN115_COOKIE 兜底；添加配置档后可多账号切换与单独绑定。' : '添加一个 115 账号，存储来源才能直挂 115。'"
          >
            <template #actions>
              <el-button size="small" type="primary" :icon="Plus" @click="openAccount(null)">添加账号</el-button>
            </template>
          </EmptyState>
        </template>
      </DataTable>
    </SectionCard>

    <!-- 账号编辑 -->
    <el-dialog v-model="accVisible" :title="accEditing ? '编辑 115 账号' : '添加 115 账号'" width="min(560px, 92vw)">
      <el-form label-position="top">
        <el-form-item label="名称">
          <el-input v-model="accForm.name" placeholder="如：主号 / 影库专号" />
        </el-form-item>
        <el-form-item label="Cookie">
          <el-input
            v-model="accForm.cookie"
            type="textarea"
            :rows="3"
            :placeholder="accEditing ? '留空表示不修改' : '粘贴浏览器里的 uid=…; cid=…; seid=…; kid=…'"
          />
        </el-form-item>
        <el-form-item label="请求 UA">
          <el-select v-model="accForm.ua" style="width: 100%">
            <el-option
              v-for="p in uaPresets"
              :key="p.value || '__default__'"
              :label="p.label"
              :value="p.value"
            />
          </el-select>
          <div class="form-hint">
            115 的直链接口对 UA 有偏好，不同配置档可以指向不同设备；默认用服务器级 UA。
          </div>
        </el-form-item>
        <el-form-item label="默认账号">
          <el-switch v-model="accForm.is_default" />
          <div class="form-hint">同一时间只有一个默认账号；媒体库未单独绑定时会用它</div>
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="accForm.is_enabled" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="accForm.remark" placeholder="可选" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button :loading="accTesting" @click="testCookie">测试 Cookie</el-button>
        <el-button @click="accVisible = false">取消</el-button>
        <el-button type="primary" :loading="accSaving" @click="saveAccount">保存</el-button>
      </template>
    </el-dialog>

    <!--
      账号管理（弹窗）：Cookie 不落明文，但「有没有配 / 上次校验成不成」在这里一次说清；
      校验会真去问 115，结果写回快照，弹窗不关。
    -->
    <el-dialog v-model="manage.visible" :title="`管理 115 账号「${manage.row?.name || ''}」`" width="min(520px, 92vw)">
      <div v-if="manage.row" class="mg-body">
        <div class="kv-list">
          <div class="kv-row"><span class="kv-key">名称</span><span class="kv-value">{{ manage.row.name }}</span></div>
          <div class="kv-row"><span class="kv-key">默认账号</span>
            <span class="kv-value">{{ manage.row.is_default ? '是（媒体库未绑定时用它）' : '否' }}</span>
          </div>
          <div class="kv-row"><span class="kv-key">状态</span>
            <span class="kv-value">
              <span class="au-badge" :class="manage.row.is_enabled ? 'au-badge-green' : 'au-badge-muted'">
                {{ manage.row.is_enabled ? '启用' : '停用' }}
              </span>
            </span>
          </div>
          <div class="kv-row"><span class="kv-key">Cookie</span>
            <span class="kv-value mono">{{ manage.row.cookie_preview || (manage.row.has_cookie ? '已配置' : '未配置') }}</span>
          </div>
          <div class="kv-row"><span class="kv-key">最近校验</span>
            <span class="kv-value">
              <template v-if="manage.row.last_verified_at">
                {{ fmtDate(manage.row.last_verified_at) }}
                <span :class="manage.row.last_verify_ok ? 'ok-text' : 'bad-text'">
                  {{ manage.row.last_verify_ok ? '有效' : '无效' }}
                </span>
              </template>
              <span v-else class="faint">未校验</span>
            </span>
          </div>
          <div v-if="manage.row.last_verify_message && !manage.row.last_verify_ok" class="kv-row">
            <span class="kv-key">失败原因</span>
            <span class="kv-value">{{ manage.row.last_verify_message }}</span>
          </div>
          <div class="kv-row"><span class="kv-key">备注</span><span class="kv-value">{{ manage.row.remark || '—' }}</span></div>
          <div class="kv-row"><span class="kv-key">创建时间</span><span class="kv-value">{{ fmtDate(manage.row.created_at) }}</span></div>
        </div>

        <p class="mg-hint">
          校验会拿这份 Cookie 去问 115（失败了也不影响面板与 EA 的现有会话，只是解不开直挂）；
          删除后绑定了它的媒体库会回退到默认账号。
        </p>
      </div>

      <template #footer>
        <div class="mg-footer">
          <el-button v-if="manage.row" type="danger" plain :icon="Trash2" @click="removeAccount(manage.row)">删除</el-button>
          <div class="mg-footer-right">
            <el-button @click="manage.visible = false">关闭</el-button>
            <template v-if="manage.row">
              <el-button :icon="ShieldCheck" @click="verify(manage.row)">校验 Cookie</el-button>
              <el-button :icon="FolderOpen" @click="browseFromManage(manage.row)">浏览目录</el-button>
              <el-button type="primary" @click="editFromManage(manage.row)">编辑</el-button>
            </template>
          </div>
        </div>
      </template>
    </el-dialog>

    <!-- 目录浏览：用该配置档实测列目录 -->
    <el-dialog v-model="browseVisible" :title="`115 目录浏览 · ${browseAccount?.name || ''}`" width="min(560px, 92vw)">
      <div class="browser">
        <div class="browser-bar">
          <nav class="crumbs" aria-label="当前路径">
            <template v-for="(p, i) in browseStack" :key="p.cid + i">
              <ChevronRight v-if="i > 0" :size="12" class="crumb-sep" />
              <button
                type="button"
                class="crumb-item"
                :class="{ 'is-current': i === browseStack.length - 1 }"
                @click="popTo(i)"
              >{{ p.name }}</button>
            </template>
          </nav>
          <el-button size="small" :icon="RefreshCw" :loading="browsing" @click="goto(browseStack[browseStack.length - 1].cid)">
            刷新
          </el-button>
        </div>
        <div class="browser-path mono" :title="currentPath()">{{ currentPath() }}</div>
        <div v-loading="browsing" class="dir-list">
          <EmptyState v-if="!browseDirs.length && !browsing" compact :icon="FolderOpen" title="该目录下没有子目录" />
          <button
            v-for="e in browseDirs"
            :key="e.cid || e.fid"
            type="button"
            class="dir-item"
            @click="enterDir(e)"
          >
            <Folder :size="14" class="dir-icon" />
            <span class="dir-name">{{ e.name }}</span>
            <ChevronRight :size="13" class="dir-arrow" />
          </button>
        </div>
      </div>
      <template #footer>
        <div class="browse-foot">
          <span class="form-hint">
            <template v-if="browseSource">Cookie 来源：{{ browseSource }}</template>
          </span>
          <el-button @click="browseVisible = false">关闭</el-button>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.stat-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 12px;
}

.flush-table :deep(.dt-cards) { padding: 12px 12px 8px; }

/* ---------- 单元格 ---------- */
.num { font-variant-numeric: tabular-nums; }
.faint { color: var(--au-text-4); }
.sub { font-size: 12px; color: var(--au-text-3); }
.ok-text { margin-left: 6px; font-weight: 600; color: var(--au-success); }
.bad-text { margin-left: 6px; font-weight: 600; color: var(--au-danger); }

/* ---------- 目录浏览 ---------- */
.browser {
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 100%;
  padding: 10px;
  background: var(--au-surface-2);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-md);
}

.browser-bar { display: flex; align-items: center; gap: 8px; }

.crumbs { display: flex; align-items: center; flex-wrap: wrap; gap: 2px 4px; flex: 1 1 auto; min-width: 0; }
.crumb-sep { color: var(--au-text-4); flex-shrink: 0; }
.crumb-item {
  padding: 2px 4px;
  font: inherit;
  font-size: 12px;
  color: var(--au-primary);
  background: none;
  border: 0;
  border-radius: var(--au-r-sm);
  cursor: pointer;
}
.crumb-item:hover { background: var(--au-primary-soft); }
.crumb-item.is-current { color: var(--au-text); font-weight: 600; cursor: default; }
.crumb-item:focus-visible { outline: 2px solid var(--au-border-focus); outline-offset: 1px; }

.browser-path {
  font-size: 11.5px;
  color: var(--au-text-4);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.dir-list {
  min-height: 60px;
  max-height: 260px;
  overflow-y: auto;
  background: var(--au-surface);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-sm);
}

.dir-item {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 8px 10px;
  font: inherit;
  font-size: 12.5px;
  text-align: left;
  color: var(--au-text-2);
  background: none;
  border: 0;
  cursor: pointer;
  transition: background var(--au-fast) var(--au-ease);
}
.dir-item + .dir-item { border-top: 1px solid var(--au-border); }
.dir-item:hover { background: var(--au-violet-soft); color: var(--au-text); }
.dir-item:focus-visible { outline: 2px solid var(--au-border-focus); outline-offset: -2px; }
.dir-icon { color: var(--au-primary); flex-shrink: 0; }
.dir-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dir-arrow { color: var(--au-text-4); flex-shrink: 0; }

.browse-foot { display: flex; align-items: center; justify-content: space-between; gap: 10px; }

/* ---------- 账号管理弹窗：详情用全局 .kv-list，只补说明与页脚布局 ---------- */
.mg-body { display: flex; flex-direction: column; gap: 12px; }
.mg-body .kv-row .kv-value { text-align: left; }
.mg-hint {
  margin: 0;
  padding: 10px 12px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--au-text-3);
  background: var(--au-surface-2);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-md);
}
.mg-footer { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
.mg-footer-right { display: flex; gap: 8px; flex-wrap: wrap; }

@media (max-width: 768px) {
  .stat-row { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
  .browser-bar { flex-wrap: wrap; }
}
</style>
