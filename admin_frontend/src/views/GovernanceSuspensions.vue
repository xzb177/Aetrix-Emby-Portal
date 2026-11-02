<script setup lang="ts">
/**
 * 治理停权（治理 Step 2）：恶意举报自动停权 + 管理员手动停权 / 撤销
 *
 * 停权只冻结治理特权（举报 / 复核投票），不扣经验、不降级、不影响播放与基础求片。
 * 与 Step 4 的「治理冻结」（/governance/freeze）是两套独立机制，这里只管 Step 2 的停权。
 *
 * 只读审计（viewer）角色：新建 / 撤销按钮置灰，服务端仍会自己判一次。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { Plus, RefreshCw, UserX } from 'lucide-vue-next'
import { PageHeader, SectionCard } from '@/components/ui'
import { parseServerTime } from '@/utils/serverTime'
import DataTable from '@/components/DataTable.vue'
import type { DataColumn } from '@/components/DataTable.vue'
import { useAuthStore } from '@/stores/auth'
import {
  createGovernanceSuspension,
  fetchGovernanceSuspensions,
  revokeGovernanceSuspension,
} from '@/api/governance'
import type { GovernanceSuspensionRow } from '@/api/governance'

const auth = useAuthStore()
const canWrite = computed(() => auth.admin?.can_write !== false)

const columns: DataColumn[] = [
  { key: 'user_id', label: '用户', width: 120, mobile: 'title' },
  { key: 'reason', label: '来源', width: 110 },
  { key: 'note', label: '备注', minWidth: 220 },
  { key: 'range', label: '起止', width: 260 },
  { key: 'state', label: '状态', width: 100 },
  { key: 'actions', label: '操作', width: 100, fixed: 'right', align: 'right' },
]

const list = ref<GovernanceSuspensionRow[]>([])
const loading = ref(false)
const loadError = ref('')
/** 'active' = 只看生效中；'' = 全部 */
const scope = ref<'active' | ''>('active')
/** 按用户 ID 筛选（空 = 不筛） */
const userFilter = ref<number | undefined>(undefined)
const rowBusyId = ref<number | null>(null)

function fmtTime(v: string | null | undefined): string {
  if (!v) return '—'
  const d = parseServerTime(v)
  if (Number.isNaN(d.getTime())) return '—'
  const p2 = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())} ${p2(d.getHours())}:${p2(d.getMinutes())}`
}

/** 状态：与后端 active_suspension 同口径（未撤销且 ends_at 在未来） */
function stateOf(s: GovernanceSuspensionRow): 'active' | 'revoked' | 'expired' {
  if (s.revoked_at) return 'revoked'
  const end = s.ends_at ? parseServerTime(s.ends_at).getTime() : NaN
  if (!Number.isNaN(end) && end > Date.now()) return 'active'
  return 'expired'
}
const STATE_LABELS = { active: '生效中', revoked: '已撤销', expired: '已到期' } as const
const STATE_BADGES = { active: 'au-badge-rose', revoked: 'au-badge-muted', expired: 'au-badge-muted' } as const

function reasonLabel(r: string): string {
  if (r === 'auto_abuse') return '自动（恶意举报）'
  if (r === 'manual') return '手动'
  return r
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    list.value = await fetchGovernanceSuspensions({
      active_only: scope.value === 'active',
      limit: 200,
      ...(userFilter.value ? { user_id: userFilter.value } : {}),
    })
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : '加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(load)

async function onRevoke(s: GovernanceSuspensionRow) {
  if (stateOf(s) !== 'active' || rowBusyId.value !== null) return
  try {
    await ElMessageBox.confirm(
      `确定撤销用户 #${s.user_id} 的治理停权吗？撤销后该用户立即恢复举报与复核投票权限。`,
      '撤销停权',
      { confirmButtonText: '撤销', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  rowBusyId.value = s.id
  try {
    await revokeGovernanceSuspension(s.id)
    ElMessage.success('已撤销停权')
    load()
  } catch {
    // 拦截器已提示（例如 409：停权已不在生效中）
    load()
  } finally {
    rowBusyId.value = null
  }
}

// ==================== 新建停权 ====================

const createVisible = ref(false)
const creating = ref(false)
const formRef = ref<FormInstance>()
const form = reactive<{ user_id: number | undefined; days: number; note: string }>({
  user_id: undefined,
  days: 30,
  note: '',
})
const rules: FormRules = {
  user_id: [{ required: true, message: '请输入用户 ID', trigger: 'blur' }],
  days: [{ required: true, message: '请输入停权天数', trigger: 'blur' }],
}

function openCreate() {
  form.user_id = userFilter.value
  form.days = 30
  form.note = ''
  createVisible.value = true
  formRef.value?.clearValidate()
}

async function submitCreate() {
  if (!formRef.value) return
  try {
    await formRef.value.validate()
  } catch {
    return
  }
  const userId = form.user_id as number
  try {
    await ElMessageBox.confirm(
      `确定暂停用户 #${userId} 的治理权限 ${form.days} 天吗？期间该用户不能举报、不能复核投票；不扣经验、不降级、不影响播放与求片。`,
      '确认停权',
      { confirmButtonText: '停权', cancelButtonText: '取消', type: 'warning', confirmButtonClass: 'el-button--danger' },
    )
  } catch {
    return
  }
  creating.value = true
  try {
    const noteText = form.note.trim()
    const row = await createGovernanceSuspension({
      user_id: userId,
      days: form.days,
      ...(noteText ? { note: noteText } : {}),
    })
    ElMessage.success(`已停权至 ${fmtTime(row.ends_at)}`)
    createVisible.value = false
    load()
  } catch {
    // 拦截器已提示（404 用户不存在 / 409 已有生效中停权 / 400 天数无效）；保留表单
  } finally {
    creating.value = false
  }
}
</script>

<template>
  <div>
    <PageHeader
      eyebrow="内容"
      title="治理停权"
      description="停权只冻结举报与复核投票权限，不扣经验、不降级、不影响播放与求片。终审驳回累计达标的举报人会被系统自动停权。"
    >
      <template #actions>
        <el-button :loading="loading" :icon="RefreshCw" @click="load">刷新</el-button>
        <el-button type="primary" :icon="Plus" :disabled="!canWrite" @click="openCreate">手动停权</el-button>
      </template>
    </PageHeader>

    <SectionCard title="停权记录" :icon="UserX" :meta="loading ? '' : `${list.length} 条`" flush>
      <div class="list-bar">
        <el-radio-group v-model="scope" aria-label="范围" @change="load">
          <el-radio-button value="active">生效中</el-radio-button>
          <el-radio-button value="">全部</el-radio-button>
        </el-radio-group>
        <div class="user-filter">
          <el-input-number
            v-model="userFilter"
            :min="1"
            :controls="false"
            placeholder="按用户 ID 筛选"
            aria-label="按用户 ID 筛选"
            class="w-user"
            @keyup.enter="load"
          />
          <el-button @click="load">筛选</el-button>
          <el-button v-if="userFilter" text @click="userFilter = undefined; load()">清除</el-button>
        </div>
        <span v-if="!canWrite" class="muted">当前为只读角色：可以查看，不能停权或撤销</span>
      </div>

      <DataTable
        :rows="list"
        :columns="columns"
        :loading="loading"
        :error="loadError"
        :empty="scope === 'active' ? '没有生效中的停权' : '还没有停权记录'"
        :empty-description="scope === 'active' ? '切到「全部」可查看已撤销 / 已到期的记录。' : ''"
        @retry="load"
      >
        <template #cell-user_id="{ row }"><span class="mono">用户 #{{ row.user_id }}</span></template>
        <template #cell-reason="{ row }">
          {{ reasonLabel(row.reason) }}
          <div v-if="row.created_by_id" class="muted">管理员 #{{ row.created_by_id }}</div>
        </template>
        <template #cell-note="{ row }"><span class="note">{{ row.note || '—' }}</span></template>
        <template #cell-range="{ row }">
          <span class="mono">{{ fmtTime(row.starts_at) }} → {{ fmtTime(row.ends_at) }}</span>
          <div v-if="row.revoked_at" class="muted">
            {{ fmtTime(row.revoked_at) }} 撤销{{ row.revoked_by_id ? `（管理员 #${row.revoked_by_id}）` : '' }}
          </div>
        </template>
        <template #cell-state="{ row }">
          <span class="au-badge" :class="STATE_BADGES[stateOf(row)]">{{ STATE_LABELS[stateOf(row)] }}</span>
        </template>
        <template #cell-actions="{ row }">
          <el-button
            v-if="stateOf(row) === 'active'"
            size="small"
            type="danger"
            :disabled="!canWrite"
            :loading="rowBusyId === row.id"
            @click="onRevoke(row)"
          >撤销</el-button>
          <span v-else class="muted">—</span>
        </template>
      </DataTable>
    </SectionCard>

    <el-dialog v-model="createVisible" title="手动停权" width="min(460px, 92vw)">
      <el-form ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent>
        <el-form-item label="用户 ID" prop="user_id">
          <el-input-number v-model="form.user_id" :min="1" :controls="false" placeholder="门户用户 ID" style="width: 100%" />
        </el-form-item>
        <el-form-item label="停权天数" prop="days">
          <el-input-number v-model="form.days" :min="1" :max="3650" :step="1" step-strictly style="width: 100%" />
        </el-form-item>
        <el-form-item label="备注（可选）" prop="note">
          <el-input v-model="form.note" type="textarea" :rows="3" maxlength="500" show-word-limit placeholder="停权原因，便于其他管理员了解背景" />
        </el-form-item>
      </el-form>
      <p class="hint">同一用户已有生效中的停权时无法重复停权，请先撤销旧记录。</p>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="danger" :loading="creating" :disabled="!canWrite" @click="submitCreate">停权…</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.list-bar {
  display: flex;
  align-items: center;
  gap: 10px 12px;
  flex-wrap: wrap;
  padding: 4px 20px 12px;
}
.user-filter { display: inline-flex; align-items: center; gap: 8px; }
.w-user { width: 160px; }
.muted { color: var(--au-text-4); font-size: 12px; }
.note { font-size: 13px; color: var(--au-text-2); overflow-wrap: anywhere; white-space: pre-wrap; }
.hint { font-size: 12px; color: var(--au-text-3); margin: 0; line-height: 1.6; }

@media (max-width: 768px) {
  .list-bar { padding: 4px 16px 12px; }
}
@media (max-width: 640px) {
  .user-filter { width: 100%; }
  .w-user { flex: 1; }
}
</style>
