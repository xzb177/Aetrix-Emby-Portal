<script setup lang="ts">
/**
 * 管理员与权限（v2.26.0）
 *
 * 后台账号就是前台账号（同一套 token 与密码），所以这一页**不新建账号**：
 * 只是把已注册的人标记成管理员，并给他一个角色。
 *
 * - 超级管理员：全部（含系统设置、上游 Key、播放与客户端策略、本页）
 * - 运营：日常运营全都能做，改不了上面那几项
 * - 只读审计：能看所有页面，任何修改都会被服务端拒绝
 *
 * 服务端才是判定的地方（backend/admin_roles.py），这一页只负责「看得懂 + 改得动」：
 * 角色说明直接来自后端下发的元数据，不在前端再维护一份。
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { toAdminError } from '@/utils/errorMessage'
import {
  AlertTriangle, CheckCircle2, Mail, RefreshCw, ShieldCheck, ShieldHalf, UserPlus, Eye, UserCog, UserX,
} from 'lucide-vue-next'
import { PageHeader, SectionCard, StatTile } from '@/components/ui'
import { fetchAdmins, grantAdmin, revokeAdmin, updateAdmin } from '@/api/admin'
import type { AdminListResponse, AdminRole, AdminRoleMeta, AdminRow } from '@/types'
import { useAuthStore } from '@/stores/auth'
import DataTable from '@/components/DataTable.vue'
import type { DataColumn } from '@/components/DataTable.vue'
import NoticePanel from '@/components/NoticePanel.vue'

const auth = useAuthStore()

const loading = ref(false)
const loadError = ref('')
const saving = ref(false)
const data = ref<AdminListResponse | null>(null)

const admins = computed(() => data.value?.admins || [])
const roles = computed<AdminRoleMeta[]>(() => data.value?.roles || [])
const superCount = computed(() => data.value?.super_count || 0)
const myId = computed(() => data.value?.me.id ?? auth.admin?.id ?? 0)
const isSuper = computed(() => auth.admin?.is_super !== false)
const inactiveCount = computed(() => admins.value.filter((a) => !a.is_active).length)

const ROLE_ICONS: Record<AdminRole, unknown> = {
  super: ShieldCheck,
  operator: ShieldHalf,
  viewer: Eye,
}

const columns = computed<DataColumn[]>(() => [
  { key: 'username', label: '管理员', minWidth: 180, mobile: 'title' },
  { key: 'role', label: '角色', width: 170 },
  { key: 'last_login_at', label: '最近登录', width: 150 },
  { key: 'is_active', label: '状态', width: 110 },
  // 角色 / 启用停用 / 撤销全部收进「管理」弹窗：行里只留一个入口
  { key: 'actions', label: '操作', width: 100 },
])

const form = ref<{ account: string; role: AdminRole }>({ account: '', role: 'operator' })

// ==================== 两个弹窗（v2.29.0） ====================
// 以前「授予管理员」是一排永远占着版面的输入框 + 下拉 + 按钮，角色又是一个行内下拉；
// 现在两件事各自一个入口按钮：页头「授予管理员」、行尾「管理」，细节都在弹窗里说清楚。
const grantVisible = ref(false)
const manage = ref({
  visible: false,
  row: null as AdminRow | null,
  role: 'operator' as AdminRole,
  active: true,
})

function openGrant() {
  form.value = { account: '', role: 'operator' }
  grantVisible.value = true
}

function openManage(row: AdminRow) {
  manage.value = { visible: true, row, role: row.admin_role, active: row.is_active }
}

/** 自己那一行不能改（服务端也拦，这里只是不让人白点） */
const manageIsSelf = computed(() => manage.value.row?.id === myId.value)
const manageLocked = computed(() => !isSuper.value || manageIsSelf.value)
const manageChanged = computed(() => {
  const row = manage.value.row
  return !!row && (row.admin_role !== manage.value.role || row.is_active !== manage.value.active)
})

/** 当前选中角色的说明（后端元数据，前端不维护第二份） */
function roleHint(value: AdminRole): string {
  return roles.value.find((r) => r.value === value)?.hint || ''
}

function roleLabel(value: AdminRole): string {
  return roles.value.find((r) => r.value === value)?.label || value
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    data.value = await fetchAdmins()
  } catch (e) {
    // 查询失败拦截器不弹提示，由表格错误态兜底
    loadError.value = e instanceof Error ? e.message : '加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(load)

/** 授权：用户名或邮箱都能填（管理员不新建账号，只标记已注册的用户） */
async function submitGrant() {
  const account = form.value.account.trim()
  if (!account) {
    ElMessage.warning('请填写要授权的用户名或邮箱')
    return
  }
  saving.value = true
  try {
    const payload = account.includes('@') ? { email: account } : { username: account }
    const res = await grantAdmin({ ...payload, role: form.value.role })
    ElMessage.success(`已把「${res.admin.username}」设为${res.admin.role_label}`)
    form.value.account = ''
    grantVisible.value = false
    await load()
  } catch (e) {
    ElMessage.error(toAdminError(e, '授权失败'))
  } finally {
    saving.value = false
  }
}

/** 保存角色 / 启用状态（只提交真正变了的字段，避免白写一次审计） */
async function saveManage() {
  const row = manage.value.row
  if (!row || !manageChanged.value) {
    manage.value.visible = false
    return
  }
  const payload: { role?: AdminRole; is_active?: boolean } = {}
  if (row.admin_role !== manage.value.role) payload.role = manage.value.role
  if (row.is_active !== manage.value.active) payload.is_active = manage.value.active
  saving.value = true
  try {
    await updateAdmin(row.id, payload)
    const bits: string[] = []
    if (payload.role) bits.push(`角色改为${roleLabel(payload.role)}`)
    if (payload.is_active !== undefined) bits.push(payload.is_active ? '已启用' : '已停用')
    ElMessage.success(`「${row.username}」${bits.join('、')}`)
    manage.value.visible = false
    await load()
  } catch (e) {
    ElMessage.error(toAdminError(e, '修改失败'))
  } finally {
    saving.value = false
  }
}

/** 撤销管理员身份（账号、会员与订阅都不受影响） */
async function revokeFromDialog() {
  const row = manage.value.row
  if (!row) return
  try {
    await ElMessageBox.confirm(
      `撤销「${row.username}」的管理员身份？账号本身、会员与订阅都不受影响。`,
      '撤销管理员', { type: 'warning' },
    )
  } catch {
    return
  }
  saving.value = true
  try {
    await revokeAdmin(row.id)
    ElMessage.success('已撤销')
    manage.value.visible = false
    await load()
  } catch (e) {
    ElMessage.error(toAdminError(e, '撤销失败'))
  } finally {
    saving.value = false
  }
}

function fmtDate(value: string | null): string {
  if (!value) return '—'
  return value.slice(0, 16).replace('T', ' ')
}
</script>

<template>
  <div class="admin-page">
    <PageHeader
      eyebrow="系统与审计"
      title="管理员与权限"
      description="后台账号就是前台账号：这里只把已注册的人标记为管理员并给他一个角色，不新建账号。"
    >
      <template #actions>
        <el-button :loading="loading" :icon="RefreshCw" @click="load">刷新</el-button>
        <!-- 授权收进弹窗：不用时它不再占着版面 -->
        <el-button type="primary" :disabled="!isSuper" :icon="UserPlus" @click="openGrant">授予管理员</el-button>
      </template>
    </PageHeader>

    <el-alert v-if="!isSuper" type="warning" :closable="false" show-icon>
      <template #title>当前账号不是超级管理员</template>
      <template #default>
        你可以查看这一页，但授权 / 改角色 / 撤销会被服务端拒绝（需要超级管理员角色）。
      </template>
    </el-alert>

    <div class="stat-row">
      <StatTile label="管理员" :value="admins.length" :icon="UserCog" suffix="名" />
      <StatTile
        label="超级管理员"
        :value="superCount"
        :icon="ShieldCheck"
        :tone="data && superCount <= 1 ? 'warn' : 'plain'"
        :hint="data && superCount <= 1 ? '只剩一名：它不能被降级或撤销' : '至少保留一名（服务端护栏）'"
      />
      <StatTile
        label="已停用"
        :value="inactiveCount"
        :icon="UserX"
        suffix="名"
        hint="停用的管理员登不进后台"
      />
    </div>

    <!-- 清单 -->
    <SectionCard
      title="管理员清单"
      :icon="ShieldCheck"
      :meta="data ? `共 ${admins.length} 名` : ''"
      description="不能改自己、不能没有超级管理员（服务端护栏）。点「管理」改角色、启用 / 停用或撤销。"
      flush
    >
      <DataTable
        :rows="admins"
        :columns="columns"
        :loading="loading"
        :error="loadError"
        empty="还没有管理员"
        row-key="id"
        @retry="load"
      >
        <template #cell-username="{ row }">
          <div class="who-cell">
            <span class="who-name">{{ row.username }}</span>
            <span v-if="row.id === myId" class="au-badge au-badge-amber">我</span>
            <span v-if="!row.is_active" class="au-badge au-badge-rose">已停用</span>
            <span v-if="row.email" class="who-mail">{{ row.email }}</span>
          </div>
        </template>

        <template #cell-role="{ row }">
          <span class="role-tag">
            <component :is="ROLE_ICONS[row.admin_role as AdminRole] || ShieldHalf" :size="13" />
            {{ row.role_label || roleLabel(row.admin_role) }}
          </span>
        </template>

        <template #cell-last_login_at="{ row }"><span class="mono">{{ fmtDate(row.last_login_at) }}</span></template>

        <template #cell-is_active="{ row }">
          <span class="au-badge" :class="row.is_active ? 'au-badge-green' : 'au-badge-muted'">
            {{ row.is_active ? '启用' : '停用' }}
          </span>
        </template>

        <template #cell-actions="{ row }">
          <!-- 角色 / 启用停用 / 撤销 都在弹窗里（原来这行是一个行内下拉 + 两个按钮） -->
          <el-button size="small" @click="openManage(row)">管理</el-button>
        </template>
      </DataTable>
    </SectionCard>

    <!--
      角色说明：来自后端元数据，前端不维护第二份。
      默认收起（v2.32.0）：说明放在清单之后——这一页要操作的是清单，说明是需要时再点开的解释。
    -->
    <NoticePanel
      title="角色说明"
      :summary="`${roles.length} 个角色：各能做什么、不能做什么`"
      :icon="ShieldCheck"
      storage-key="admins-roles"
    >
      <div class="role-grid">
        <div v-for="role in roles" :key="role.value" class="role-card">
          <div class="role-head">
            <span class="role-icon"><component :is="ROLE_ICONS[role.value]" :size="16" /></span>
            <strong>{{ role.label }}</strong>
            <span class="role-key mono">{{ role.value }}</span>
          </div>
          <p class="role-hint">{{ role.hint }}</p>
        </div>
      </div>
    </NoticePanel>

    <!-- 三条护栏：默认收起（v2.32.0）——它是解释，不是这一页要操作的东西 -->
    <NoticePanel
      title="三条护栏（服务端强制执行）"
      summary="不能改自己 · 不能没有超级管理员 · 停用账号不能当管理员"
      :icon="AlertTriangle"
      storage-key="admins-guards"
    >
      <div class="tips">
        <ul>
          <li><CheckCircle2 :size="13" /> <b>不能改自己</b>：把自己的角色降下去或撤销自己，等于把自己关在门外。</li>
          <li><CheckCircle2 :size="13" /> <b>不能没有超级管理员</b>：最后一名 super 既不能降级也不能撤销。</li>
          <li><CheckCircle2 :size="13" /> <b>停用账号不能当管理员</b>：那种号永远登不进来，先启用再说。</li>
        </ul>
        <p class="tips-foot">
          只读角色在服务端拦截一切写操作（包括扫描、扫描与挂载体检），不是「界面置灰」而已。
        </p>
      </div>
    </NoticePanel>
    <!-- 授予管理员（弹窗）：账号需要先在站点注册过 -->
    <el-dialog v-model="grantVisible" title="授予管理员" width="min(460px, 92vw)">
      <el-form label-position="top">
        <el-form-item label="用户名或邮箱">
          <el-input
            v-model="form.account"
            placeholder="已注册的用户名或邮箱"
            clearable
            @keyup.enter="submitGrant"
          >
            <template #prefix><Mail :size="14" /></template>
          </el-input>
          <p class="form-hint">这里不新建账号：只把已经在站点注册过的人标记为管理员。</p>
        </el-form-item>
        <el-form-item label="角色">
          <el-select v-model="form.role" class="w-full">
            <el-option v-for="role in roles" :key="role.value" :label="role.label" :value="role.value">
              <span class="opt-row">
                <component :is="ROLE_ICONS[role.value]" :size="14" />{{ role.label }}
              </span>
            </el-option>
          </el-select>
          <p class="form-hint">{{ roleHint(form.role) }}</p>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="grantVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submitGrant">授权</el-button>
      </template>
    </el-dialog>

    <!-- 管理某个管理员（弹窗）：角色 + 启用状态 + 撤销，护栏也在页面上说清楚 -->
    <el-dialog v-model="manage.visible" :title="`管理「${manage.row?.username || ''}」`" width="min(500px, 92vw)">
      <div v-if="manage.row" class="manage-body">
        <div class="kv-list">
          <div class="kv-row"><span class="kv-key">账号</span>
            <span class="kv-value">{{ manage.row.username }}{{ manage.row.id === myId ? '（我）' : '' }}</span>
          </div>
          <div class="kv-row"><span class="kv-key">邮箱</span><span class="kv-value">{{ manage.row.email || '—' }}</span></div>
          <div class="kv-row"><span class="kv-key">当前角色</span><span class="kv-value">{{ manage.row.role_label }}</span></div>
          <div class="kv-row"><span class="kv-key">最近登录</span><span class="kv-value">{{ fmtDate(manage.row.last_login_at) }}</span></div>
          <div class="kv-row"><span class="kv-key">加入时间</span><span class="kv-value">{{ fmtDate(manage.row.created_at) }}</span></div>
        </div>

        <el-form label-position="top" class="manage-form">
          <el-form-item label="角色">
            <el-select v-model="manage.role" :disabled="manageLocked" class="w-full">
              <el-option v-for="role in roles" :key="role.value" :label="role.label" :value="role.value" />
            </el-select>
            <p class="form-hint">{{ roleHint(manage.role) }}</p>
          </el-form-item>
          <el-form-item label="账号状态">
            <el-switch
              v-model="manage.active"
              :disabled="manageLocked"
              active-text="启用"
              inactive-text="停用（登不进来）"
            />
          </el-form-item>
        </el-form>

        <el-alert v-if="manageLocked" type="warning" :closable="false" show-icon>
          <template #title>{{ manageIsSelf ? '这是你自己的账号' : '需要超级管理员' }}</template>
          <template #default>
            {{ manageIsSelf
              ? '不能把自己的角色降下去或撤销自己——那等于把自己关在门外。'
              : '你可以查看，但改角色 / 停用 / 撤销会被服务端拒绝。' }}
          </template>
        </el-alert>
      </div>

      <template #footer>
        <div class="manage-footer">
          <el-button
            type="danger"
            :disabled="manageLocked"
            :loading="saving"
            @click="revokeFromDialog"
          >
            撤销管理员
          </el-button>
          <div class="manage-footer-right">
            <el-button @click="manage.visible = false">取消</el-button>
            <el-button
              type="primary"
              :loading="saving"
              :disabled="manageLocked || !manageChanged"
              @click="saveManage"
            >
              保存
            </el-button>
          </div>
        </div>
      </template>
    </el-dialog>

  </div>
</template>

<style scoped>
.stat-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
}

.role-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 12px;
}

.role-card {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 14px;
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-md);
  background: var(--au-surface-2);
}
.role-head { display: flex; align-items: center; gap: 8px; color: var(--au-text); }

.role-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border-radius: var(--au-r-sm);
  background: var(--au-primary-soft);
  color: var(--au-primary);
}

.role-key { margin-left: auto; font-size: 12px; color: var(--au-text-4); }
.role-hint { margin: 0; font-size: 12.5px; color: var(--au-text-3); line-height: 1.6; }

.who-cell { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.who-name { font-weight: 600; color: var(--au-text); }
.who-mail { font-size: 12px; color: var(--au-text-3); }

.role-tag { display: inline-flex; align-items: center; gap: 6px; color: var(--au-text-2); }
.role-tag :deep(svg) { color: var(--au-text-4); flex-shrink: 0; }
.w-full { width: 100%; }

/* 弹窗：详情用全局 .kv-list，只补布局 */
.manage-body { display: flex; flex-direction: column; gap: 14px; }
.manage-body .kv-row .kv-value { text-align: left; }
.manage-form :deep(.el-form-item) { margin-bottom: 12px; }
.manage-footer { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
.manage-footer-right { display: flex; gap: 8px; }
.opt-row { display: inline-flex; align-items: center; gap: 6px; }

.tips ul { margin: 0; padding-left: 4px; list-style: none; display: flex; flex-direction: column; gap: 8px; }
.tips li {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  font-size: 13px;
  color: var(--au-text-2);
}
.tips li :deep(svg) { color: var(--au-success); flex-shrink: 0; margin-top: 3px; }
.tips-foot { margin: 12px 0 0; font-size: 12px; color: var(--au-text-3); }

@media (max-width: 640px) {
  .stat-row { grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; }
  .manage-footer { flex-direction: column-reverse; align-items: stretch; }
  .manage-footer-right :deep(.el-button) { flex: 1; margin-left: 0; }
}
</style>
