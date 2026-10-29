<script setup lang="ts">
/**
 * 公益服 · 抽奖黑名单（三重门·资格门）
 *
 * 被拉黑的用户无法参加任何群抽奖（参加时 backend/lottery.py::is_blacklisted 拦截，
 * 已参加、已中奖的历史记录不受影响）。后端以站内用户 ID（web_users.id）为准：
 * 添加 POST /welfare/lottery/blacklist {user_id, reason}，移除 DELETE .../{user_id}。
 * 添加时可按用户名 / 邮箱搜索（复用用户管理的 /users 列表口），也可直接输入用户 ID。
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Ban, Plus, RefreshCw } from 'lucide-vue-next'
import { PageHeader, SectionCard } from '@/components/ui'
import { useBreakpoint } from '@/composables/useBreakpoint'
import { useAuthStore } from '@/stores/auth'
import { toAdminError } from '@/utils/errorMessage'
import DataTable from '@/components/DataTable.vue'
import type { DataColumn } from '@/components/DataTable.vue'
import { fetchUsers } from '@/api/admin'
import {
  addLotteryBlacklist,
  fetchLotteryBlacklist,
  removeLotteryBlacklist,
  type LotteryBlacklistRow,
} from '@/api/welfare'

const REASON_MAX = 200 // 后端 LotteryBlacklistAdd.reason max_length=200
const fmtTime = (v?: string | null) => (v ? String(v).replace('T', ' ').slice(0, 19) : '—')

const { isPhone } = useBreakpoint()
const auth = useAuthStore()
const canWrite = computed(() => auth.admin?.can_write !== false)

// ---------- 列表 ----------
const loading = ref(false)
const loadError = ref('')
const rows = ref<LotteryBlacklistRow[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = 20

const columns = computed<DataColumn[]>(() => [
  { key: 'username', label: '用户', minWidth: 160, mobile: 'title' },
  { key: 'user_id', label: '用户ID', width: 100 },
  { key: 'reason', label: '原因', minWidth: 200 },
  { key: 'created_at', label: '加入时间', width: 170 },
  { key: 'actions', label: '操作', width: 100, fixed: 'right', align: 'right' },
])

const load = async () => {
  loading.value = true
  loadError.value = ''
  try {
    const res = await fetchLotteryBlacklist({ page: page.value, page_size: pageSize })
    rows.value = res.items ?? []
    total.value = res.total ?? 0
    // 删掉最后一页的最后一条后，页码回退一页
    if (!rows.value.length && page.value > 1 && total.value > 0) {
      page.value -= 1
      await load()
    }
  } catch (e) {
    loadError.value = toAdminError(e, '加载失败')
  } finally {
    loading.value = false
  }
}

// ---------- 添加 ----------
interface UserOption { value: number; label: string }
const addVisible = ref(false)
const adding = ref(false)
const addForm = ref<{ user_id: number | null; reason: string }>({ user_id: null, reason: '' })
const userOptions = ref<UserOption[]>([])
const searchingUsers = ref(false)

const openAdd = () => {
  addForm.value = { user_id: null, reason: '' }
  userOptions.value = []
  addVisible.value = true
}

/** 远程搜索：纯数字额外给出「按用户 ID」选项，用户名 / 邮箱走用户列表口 */
const searchUsers = async (query: string) => {
  const q = query.trim()
  if (!q) { userOptions.value = []; return }
  const opts: UserOption[] = []
  if (/^\d+$/.test(q) && Number(q) > 0) opts.push({ value: Number(q), label: `用户 ID ${q}` })
  searchingUsers.value = true
  try {
    const res = await fetchUsers({ search: q, limit: 10, offset: 0 })
    for (const u of res.users ?? []) {
      if (opts.some(o => o.value === u.id)) {
        const hit = opts.find(o => o.value === u.id)
        if (hit) hit.label = `${u.username}（ID ${u.id}）`
        continue
      }
      opts.push({ value: u.id, label: `${u.username}（ID ${u.id}${u.email ? ` · ${u.email}` : ''}）` })
    }
  } catch {
    /* 搜索失败已由拦截器提示；保留纯数字 ID 选项，仍可直接按 ID 添加 */
  } finally {
    searchingUsers.value = false
    userOptions.value = opts
  }
}

const submitAdd = async () => {
  if (adding.value) return
  const uid = addForm.value.user_id
  if (!uid || uid <= 0) { ElMessage.warning('请选择或输入要拉黑的用户'); return }
  const label = userOptions.value.find(o => o.value === uid)?.label ?? `用户 ID ${uid}`
  try {
    await ElMessageBox.confirm(`把 ${label} 加入抽奖黑名单？加入后该用户无法参加任何群抽奖。`, '确认', { type: 'warning' })
  } catch { return }
  adding.value = true
  try {
    await addLotteryBlacklist({ user_id: uid, reason: addForm.value.reason.trim() })
    ElMessage.success('已加入黑名单')
    addVisible.value = false
    page.value = 1
    load()
  } catch {
    /* 写操作失败由请求拦截器统一弹错（如「用户不存在」「该用户已在黑名单中」） */
  } finally {
    adding.value = false
  }
}

// ---------- 移除 ----------
const handleRemove = async (row: LotteryBlacklistRow) => {
  const name = row.username ? `${row.username}（ID ${row.user_id}）` : `用户 ID ${row.user_id}`
  try {
    await ElMessageBox.confirm(`把 ${name} 移出抽奖黑名单？移出后可再次参加群抽奖。`, '确认', { type: 'warning' })
  } catch { return }
  try {
    await removeLotteryBlacklist(row.user_id)
    ElMessage.success('已移出黑名单')
    load()
  } catch {
    /* 写操作失败由请求拦截器统一弹错 */
  }
}

onMounted(load)
</script>

<template>
  <div class="admin-page">
    <PageHeader eyebrow="内容" title="抽奖黑名单" description="被拉黑的用户无法参加任何群抽奖；已有的参与与中奖记录不受影响">
      <template #actions>
        <el-button :icon="RefreshCw" :loading="loading" @click="load">刷新</el-button>
        <el-button type="primary" :icon="Plus" :disabled="!canWrite" @click="openAdd">添加</el-button>
      </template>
    </PageHeader>

    <SectionCard title="黑名单" :icon="Ban" :meta="`共 ${total} 人`" flush>
      <DataTable
        :columns="columns"
        :rows="rows"
        :loading="loading"
        :error="loadError"
        empty="黑名单为空"
        empty-description="点右上角「添加」按用户名或用户 ID 拉黑。"
        :pagination="{ total, page, pageSize }"
        @retry="load"
        @page-change="(p: number) => { page = p; load() }"
      >
        <template #cell-username="{ row }">{{ row.username || '（用户已不存在）' }}</template>
        <template #cell-reason="{ row }">{{ row.reason || '—' }}</template>
        <template #cell-created_at="{ row }">{{ fmtTime(row.created_at) }}</template>
        <template #cell-actions="{ row }">
          <el-button link type="danger" :disabled="!canWrite" @click="handleRemove(row)">移除</el-button>
        </template>
      </DataTable>
    </SectionCard>

    <el-dialog v-model="addVisible" title="加入抽奖黑名单" width="min(480px, 92vw)" append-to-body>
      <el-form :label-position="isPhone ? 'top' : 'right'" label-width="70px" @submit.prevent>
        <el-form-item label="用户" required>
          <el-select
            v-model="addForm.user_id"
            filterable
            remote
            clearable
            :remote-method="searchUsers"
            :loading="searchingUsers"
            placeholder="输入用户名 / 邮箱搜索，或直接输入用户 ID"
            no-data-text="没有匹配的用户"
            style="width: 100%"
          >
            <el-option v-for="o in userOptions" :key="o.value" :label="o.label" :value="o.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="原因">
          <el-input
            v-model="addForm.reason"
            type="textarea"
            :rows="2"
            :maxlength="REASON_MAX"
            show-word-limit
            placeholder="可选，例如：多号刷奖"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="addVisible = false">取消</el-button>
        <el-button type="danger" :loading="adding" :disabled="!canWrite" @click="submitAdd">加入黑名单</el-button>
      </template>
    </el-dialog>
  </div>
</template>
