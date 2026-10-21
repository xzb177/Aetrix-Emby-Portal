<script setup lang="ts">
/**
 * 媒体库可见范围（v2.43.0）：服务器默认范围 + 指定用户单独覆盖
 *
 * 这一页回答三个问题，顺序就是管理员用它的顺序：
 *
 * 1. **全站默认让谁看到哪些库**（默认范围卡）。**默认关闭** = 所有启用的库照旧
 *    全部可见，升级上来的部署行为与升级前逐字一致。
 * 2. **谁被单独开了一张单子**（覆盖表）。覆盖开着 = 看自己那份；关掉 = 恢复
 *    跟随服务器默认（列表留着，方便再打开）。
 * 3. **现在真的生效了吗**（统计与警示）。开启却一个库都没选 / 选的库被删光，
 *    后端会退回「不过滤」而不是把媒体库清空——这里如实显示成「未生效」。
 *
 * 一条必须说清的口径：**工作人员账号不受可见范围限制**（排障要看到全部库），
 * 与「防共享」对管理员豁免是同一个理由。所以勾选范围时不提示「这会挡住管理员」，
 * 反而要在说明里点明。
 *
 * 判定与生效在后端 `backend/library_scope.py`（客户端列表 / 搜索 / 继续观看 /
 * 最近上新 / 接下来看都按它过滤），本页只做配置与展示。
 *
 * v2.54（暗房影院）：PageHeader + StatTile；默认范围是一张 SectionCard（有未保存改动时琥珀描边，
 * 保存 / 还原在卡片底栏），覆盖表是 flush SectionCard，「新增覆盖」在标题行右侧；
 * 操作列改用 actions 键，手机卡片里按钮落到底部操作区。首屏骨架 + 加载失败可重试。
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import {
  AlertTriangle, Eye, Library, RefreshCw, Save, Plus, ShieldCheck, TriangleAlert, Trash2, UserCog,
} from 'lucide-vue-next'
import { EmptyState, PageHeader, SectionCard, StatTile } from '@/components/ui'
import {
  fetchLibraryScope,
  removeLibraryScopeUser,
  saveLibraryScopeDefault,
  saveLibraryScopeUser,
  type LibraryScopeOverride,
  type LibraryScopeResponse,
} from '@/api/admin'
import { useAuthStore } from '@/stores/auth'
import { confirmIrreversible } from '@/composables/useDangerOps'
import DataTable from '@/components/DataTable.vue'
import type { DataColumn } from '@/components/DataTable.vue'

/** 覆盖表：用户（标题）/ 状态 / 名单 / 操作 */
const columns: DataColumn[] = [
  { key: 'username', label: '用户', width: 160, mobile: 'title' },
  { key: 'state', label: '状态', width: 130 },
  { key: 'libraries', label: '可见媒体库', minWidth: 260 },
  // actions：手机卡片里渲染到底部操作区（action 会退化成一行「标签 / 值」，按钮不好按）
  { key: 'actions', label: '操作', width: 230 },
]

const data = ref<LibraryScopeResponse | null>(null)
const loading = ref(false)
const saving = ref(false)
/** 配置加载失败（整页没有可展示的数据） */
const loadError = ref(false)

/** 本地草稿：改完点「保存」才写后端，不动后端就不算改 */
const draftEnabled = ref(false)
const draftIds = ref<number[]>([])
const filter = ref('')

const auth = useAuthStore()
/** 与「系统设置」同一口径：有写权限才能改（普通只读账号可读） */
const canWrite = computed(() => auth.admin?.can_write !== false)
const WRITE_HINT = '当前账号是只读角色，下面的按钮已置灰，但配置仍然可读'

const sameIds = (a: number[], b: number[]) => {
  const x = [...a].sort((m, n) => m - n)
  const y = [...b].sort((m, n) => m - n)
  return x.length === y.length && x.every((v, i) => v === y[i])
}

const dirty = computed(() => {
  const cur = data.value?.default
  if (!cur) return false
  return cur.enabled !== draftEnabled.value || !sameIds(cur.library_ids, draftIds.value)
})

/** 当前草稿勾选的库（用来在列表里画出「已选」以及算条数） */
const libraryOptions = computed(() => data.value?.libraries ?? [])
const visibleOptions = computed(() => {
  const kw = filter.value.trim().toLowerCase()
  if (!kw) return libraryOptions.value
  return libraryOptions.value.filter((lib) => lib.name.toLowerCase().includes(kw))
})
const users = computed(() => data.value?.users ?? [])

const overrideRows = computed(() => {
  const overrides = data.value?.overrides ?? {}
  const byId = new Map(users.value.map((u) => [u.id, u]))
  return Object.entries(overrides).map(([uid, value]) => {
    const id = Number(uid)
    const user = byId.get(id)
    return {
      user_id: id,
      username: user?.username ?? `用户 #${uid}`,
      is_staff: !!user?.is_staff,
      state: value.enabled ? 'enabled' : 'disabled',
      state_label: value.enabled ? '单独覆盖' : '跟随默认',
      libraries: namesOf(value.library_ids),
      _override: value,
    }
  })
})

function namesOf(ids: number[]): string[] {
  const map = new Map(libraryOptions.value.map((lib) => [lib.id, lib.name]))
  return ids.map((id) => map.get(id) ?? `已删除的库 #${id}`)
}

const statDefault = computed(() => {
  const d = data.value?.default
  if (!d) return '—'
  if (!d.enabled) return '未限制'
  return d.active ? `限定 ${d.library_ids.length} 个` : '未生效'
})

async function load() {
  loading.value = true
  loadError.value = false
  try {
    data.value = await fetchLibraryScope()
    draftEnabled.value = data.value.default.enabled
    draftIds.value = [...data.value.default.library_ids]
  } catch {
    // 错误提示由 HTTP 拦截器统一处理；这里只记下失败，给出重试入口
    loadError.value = true
  } finally {
    loading.value = false
  }
}

onMounted(load)

/** 默认范围瓦片的提示色：开着却没生效 = 警示；开着且生效 = 信息；关着 = 不染色 */
const defaultTone = computed(() => {
  const d = data.value?.default
  if (!d?.enabled) return 'plain' as const
  return d.active ? ('info' as const) : ('warn' as const)
})
const enabledLibraries = computed(() => libraryOptions.value.filter((l) => l.is_enabled).length)

async function saveDefault() {
  saving.value = true
  try {
    const res = await saveLibraryScopeDefault({
      enabled: draftEnabled.value,
      library_ids: draftIds.value,
    })
    data.value = { ...(data.value as LibraryScopeResponse), ...res.policy }
    // 以后端返回的**真实生效值**回填（非法输入后端会 400，不会存半个坏配置）
    draftEnabled.value = res.policy.default.enabled
    draftIds.value = [...res.policy.default.library_ids]
    ElMessage.success('默认可见范围已保存并立即生效')
  } catch {
    /* 拦截器已提示（启用却一个都没勾会走 400） */
  } finally {
    saving.value = false
  }
}

function revert() {
  if (!data.value) return
  draftEnabled.value = data.value.default.enabled
  draftIds.value = [...data.value.default.library_ids]
}

function toggleAll() {
  const all = visibleOptions.value.map((lib) => lib.id)
  if (!all.length) return
  const everything = sameIds(draftIds.value, all)
  draftIds.value = everything
    ? draftIds.value.filter((id) => !all.includes(id))
    : [...new Set([...draftIds.value, ...all])]
}

/** 勾选框改一个 id：写进默认范围那份草稿（点「保存」才落库） */
function setDraft(id: number, on: boolean) {
  draftIds.value = on
    ? [...new Set([...draftIds.value, id])]
    : draftIds.value.filter((i) => i !== id)
}


// ==================== 逐用户覆盖 ====================

const dialogVisible = ref(false)
const savingUser = ref(false)
/** 正在编辑的用户 id（0 = 新增）。新增时可以换人，编辑时锁死 */
const editingUserId = ref(0)
const formUserId = ref<number | null>(null)
const formEnabled = ref(true)
const formIds = ref<number[]>([])

const isStaffUser = computed(
  () => users.value.find((u) => u.id === formUserId.value)?.is_staff ?? false,
)

/** 同上，覆盖对话框里那一份（勾选可见媒体库） */
function setForm(id: number, on: boolean) {
  formIds.value = on
    ? [...new Set([...formIds.value, id])]
    : formIds.value.filter((i) => i !== id)
}

/** 已经有覆盖的人不进「新增」下拉：重复添加只会静默覆盖旧名单 */
const candidateUsers = computed(() => {
  if (editingUserId.value) return users.value
  const overridden = new Set(Object.keys(data.value?.overrides ?? {}).map(Number))
  return users.value.filter((u) => !overridden.has(u.id))
})

const formValid = computed(
  () => !!formUserId.value && (!formEnabled.value || formIds.value.length > 0),
)

function openCreate() {
  editingUserId.value = 0
  formUserId.value = null
  formEnabled.value = true
  formIds.value = []
  dialogVisible.value = true
}

function openEdit(row: { user_id: number; _override: LibraryScopeOverride }) {
  editingUserId.value = row.user_id
  formUserId.value = row.user_id
  formEnabled.value = row._override.enabled
  formIds.value = [...row._override.library_ids]
  dialogVisible.value = true
}

async function saveUserScope() {
  const userId = formUserId.value
  if (!userId || !formValid.value) return
  savingUser.value = true
  try {
    const res = await saveLibraryScopeUser(userId, {
      enabled: formEnabled.value,
      library_ids: formIds.value,
    })
    data.value = { ...(data.value as LibraryScopeResponse), ...res.policy }
    dialogVisible.value = false
    ElMessage.success(
      res.policy.overrides[String(userId)]?.enabled
        ? '该用户的单独覆盖已生效'
        : '已恢复跟随服务器默认',
    )
  } catch {
    /* 拦截器已提示 */
  } finally {
    savingUser.value = false
  }
}

async function toggleOverride(row: { user_id: number; _override: LibraryScopeOverride }) {
  if (!canWrite.value) return
  try {
    const res = await saveLibraryScopeUser(row.user_id, {
      enabled: !row._override.enabled,
      library_ids: row._override.library_ids,
    })
    data.value = { ...(data.value as LibraryScopeResponse), ...res.policy }
    ElMessage.success(res.policy.overrides[String(row.user_id)]?.enabled ? '已开启单独覆盖' : '已恢复跟随默认')
  } catch {
    /* 拦截器已提示 */
  }
}

async function removeOverride(row: { username: string; user_id: number }) {
  const ok = await confirmIrreversible(
    `删除后「${row.username}」完全恢复跟随服务器默认，且这份名单不再保留。`,
    '删除覆盖',
    `删除 ${row.username} 的可见范围覆盖`,
  )
  if (!ok) return
  try {
    const res = await removeLibraryScopeUser(row.user_id)
    data.value = { ...(data.value as LibraryScopeResponse), ...res.policy }
    ElMessage.success('已删除该用户的单独覆盖')
  } catch {
    /* 拦截器已提示 */
  }
}
</script>

<template>
  <div class="admin-page scope-page">
    <PageHeader
      eyebrow="媒体与交付"
      title="媒体库可见范围"
      description="服务器默认范围 + 指定用户单独覆盖。默认关闭——所有启用的媒体库照旧全部可见；工作人员账号不受限制。"
    >
      <template #actions>
        <el-button :icon="RefreshCw" :loading="loading" @click="load">刷新</el-button>
      </template>
    </PageHeader>

    <!-- 首屏骨架 / 加载失败 -->
    <div v-if="!data && loading" class="scope-skeleton" aria-busy="true" aria-label="加载中">
      <div v-for="n in 4" :key="n" class="au-skeleton sk-tile" />
      <div class="au-skeleton sk-wide" />
    </div>

    <SectionCard v-else-if="!data">
      <EmptyState
        :icon="AlertTriangle"
        title="可见范围配置加载失败"
        :description="loadError ? '网络或服务暂时不可用，稍后重试。' : '还没有拿到配置。'"
      >
        <template #actions><el-button :loading="loading" @click="load">重试</el-button></template>
      </EmptyState>
    </SectionCard>

    <template v-else>
      <section class="stat-row" aria-label="可见范围概况">
        <StatTile label="媒体库" :value="data.counts.libraries" :icon="Eye" :hint="`其中 ${enabledLibraries} 个启用中`" />
        <StatTile
          label="默认范围"
          :value="statDefault"
          :icon="ShieldCheck"
          :tone="defaultTone"
          hint="「未限制」= 所有启用的库都可见"
        />
        <StatTile
          label="单独覆盖"
          :value="data.counts.overrides"
          suffix="人"
          :icon="UserCog"
          :tone="data.counts.overrides > 0 ? 'info' : 'plain'"
          hint="关掉覆盖就回到默认范围"
        />
        <StatTile label="工作人员" value="豁免" :icon="TriangleAlert" hint="排障需要看全部库，不受本设置限制" />
      </section>

      <!-- 默认范围：本地草稿，点「保存」才写后端；有改动时卡片琥珀描边 -->
      <SectionCard :icon="ShieldCheck" :tone="dirty ? 'accent' : 'default'">
        <template #title>
          服务器默认范围
          <span v-if="dirty" class="au-badge badge-warn">有未保存的改动</span>
        </template>

        <div class="scope-body">
          <el-alert
            v-if="data.default.enabled && !data.default.active"
            type="warning"
            :closable="false"
            show-icon
          >
            这份范围<strong>当前没有生效</strong>：勾选的媒体库已经不存在了（可能被删掉）。
            此时按「不处理」退回全部可见，而不是把所有人的媒体库清空。请重新勾选后再保存。
          </el-alert>

          <el-alert v-if="!canWrite" type="info" :closable="false" show-icon>
            {{ WRITE_HINT }}。下面的说明仍然可读。
          </el-alert>

          <p class="ls-hint">
            打开后，<strong>没有单独覆盖</strong>的用户只看到勾选的媒体库；关闭则所有人
            （除工作人员外）照旧看到全部启用的库。勾选只决定「列表里显不显示」，
            判定发生在后端：客户端列表、搜索、继续观看、最近上新、接下来看都按它过滤。
          </p>

          <div class="ls-switch-row">
            <el-switch
              v-model="draftEnabled"
              :disabled="!canWrite"
              active-text="限定可见范围"
              inactive-text="不限制（全部可见）"
            />
            <span v-if="draftEnabled" class="au-badge au-badge-amber">已勾选 {{ draftIds.length }} 个媒体库</span>
          </div>

          <div v-show="draftEnabled" class="ls-pick">
            <div class="ls-pick-head">
              <el-input v-model="filter" class="f-search" placeholder="筛选媒体库名称" clearable size="small" />
              <el-button size="small" :disabled="!canWrite || !visibleOptions.length" @click="toggleAll">
                {{ visibleOptions.length ? '全选 / 取消当前筛选结果' : '无可选项' }}
              </el-button>
            </div>
            <div class="ls-pick-list">
              <EmptyState
                v-if="!libraryOptions.length"
                compact
                :icon="Library"
                title="站点还没有任何媒体库"
                description="先到「媒体与交付 → 媒体库」建库再回来配置。"
              />
              <template v-else>
                <div v-for="lib in visibleOptions" :key="lib.id" class="ls-pick-item">
                  <el-checkbox
                    :model-value="draftIds.includes(lib.id)"
                    :disabled="!canWrite"
                    @change="(v: boolean | string | number) => setDraft(lib.id, v === true)"
                  >
                    <span class="ls-lib-name">{{ lib.name }}</span>
                  </el-checkbox>
                  <span class="ls-lib-meta">
                    <span v-if="!lib.is_enabled" class="au-badge au-badge-muted">已停用</span>
                    <span v-else-if="lib.is_virtual" class="au-badge au-badge-info">虚拟库</span>
                    <span class="ls-count">{{ lib.item_count }} 条</span>
                  </span>
                </div>
                <EmptyState v-if="!visibleOptions.length" compact :title="`没有名称匹配「${filter}」的媒体库`" />
              </template>
            </div>
          </div>
        </div>

        <template #footer>
          <div class="ls-actions">
            <el-button type="primary" :icon="Save" :disabled="!canWrite || !dirty" :loading="saving" @click="saveDefault">
              保存默认范围
            </el-button>
            <el-button :disabled="!canWrite || !dirty" @click="revert">还原</el-button>
            <span class="ls-actions-hint">
              保存后对<strong>未单独覆盖</strong>的用户生效（工作人员不受本设置限制）。
              已有 {{ data.counts.overrides }} 个用户挂了自己的名单。
            </span>
          </div>
        </template>
      </SectionCard>

      <!-- 单独覆盖：用户看自己那份名单，优先于默认范围 -->
      <SectionCard
        title="指定用户单独覆盖"
        :icon="UserCog"
        :meta="overrideRows.length ? `${overrideRows.length} 人` : ''"
        description="这里列出的用户看自己那份名单，优先于服务器默认范围。拨到「跟随默认」= 关掉覆盖、恢复按默认范围走；名单会留着，方便再打开。"
        flush
      >
        <template #actions>
          <el-button size="small" type="primary" :icon="Plus" :disabled="!canWrite" @click="openCreate">新增覆盖</el-button>
        </template>

        <DataTable
          class="flush-table"
          :rows="overrideRows"
          :columns="columns"
          :loading="loading"
          empty="还没有给任何用户单独设置"
        >
          <template #cell-username="{ row }">
            <span class="ls-user">{{ row.username }}</span>
            <span v-if="row.is_staff" class="au-badge au-badge-info">工作人员</span>
          </template>

          <template #cell-state="{ row }">
            <span class="au-badge" :class="row.state === 'enabled' ? 'au-badge-amber' : 'au-badge-muted'">
              {{ row.state_label }}
            </span>
          </template>

          <template #cell-libraries="{ row }">
            <span v-if="!row.libraries.length" class="faint">—</span>
            <template v-else>{{ row.libraries.join('、') }}</template>
          </template>

          <template #cell-actions="{ row }">
            <div class="ls-row-actions">
              <el-button size="small" :disabled="!canWrite" @click="openEdit(row)">编辑</el-button>
              <el-button size="small" :disabled="!canWrite" @click="toggleOverride(row)">
                {{ row.state === 'enabled' ? '恢复跟随默认' : '开启覆盖' }}
              </el-button>
              <el-button
                size="small"
                type="danger"
                plain
                :icon="Trash2"
                aria-label="删除覆盖"
                title="删除覆盖"
                :disabled="!canWrite"
                @click="removeOverride(row)"
              />
            </div>
          </template>

          <template #empty>
            <EmptyState
              compact
              :icon="UserCog"
              title="还没有给任何用户单独设置"
              description="所有人都按服务器默认范围看媒体库。"
            />
          </template>
        </DataTable>
      </SectionCard>
    </template>

    <!-- 新增 / 编辑覆盖 -->
    <el-dialog
      v-model="dialogVisible"
      :title="editingUserId ? '编辑可见范围覆盖' : '新增可见范围覆盖'"
      width="min(520px, 92vw)"
      class="ls-dialog"
    >
      <el-form label-position="top">
        <el-form-item label="用户">
          <el-select
            v-model="formUserId"
            :placeholder="candidateUsers.length ? '搜索并选择用户' : '所有用户都已有覆盖：先删掉一条再新增'"
            filterable
            :disabled="!!editingUserId || !canWrite"
            style="width: 100%"
          >
            <el-option
              v-for="u in candidateUsers"
              :key="u.id"
              :value="u.id"
              :label="`${u.username}（#${u.id}）`"
            />
          </el-select>
          <span v-if="isStaffUser" class="ls-hint">
            这是工作人员账号：<strong>它不受可见范围限制</strong>，这条覆盖不会改变他实际看到的库。
          </span>
        </el-form-item>

        <el-form-item label="覆盖开关">
          <el-switch
            v-model="formEnabled"
            :disabled="!canWrite"
            active-text="单独覆盖"
            inactive-text="跟随默认"
          />
        </el-form-item>

        <el-form-item v-if="formEnabled" label="可见媒体库">
          <div class="ls-pick-list ls-dialog-list">
            <div v-for="lib in libraryOptions" :key="lib.id" class="ls-pick-item">
              <el-checkbox
                :model-value="formIds.includes(lib.id)"
                :disabled="!canWrite"
                @change="(v: boolean | string | number) => setForm(lib.id, v === true)"
              >
                <span class="ls-lib-name">{{ lib.name }}</span>
              </el-checkbox>
              <span class="ls-lib-meta">
                <span v-if="!lib.is_enabled" class="au-badge au-badge-muted">已停用</span>
              </span>
            </div>
            <div v-if="!libraryOptions.length" class="ls-empty">站点还没有任何媒体库</div>
          </div>
          <span v-if="formEnabled" class="ls-hint">已勾选 {{ formIds.length }} 个</span>
        </el-form-item>

        <p v-else class="ls-hint">
          关闭覆盖后该用户按服务器默认范围看媒体库，这里的选择会被保留。
        </p>
      </el-form>

      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button
          type="primary"
          :icon="Save"
          :disabled="!canWrite || !formValid"
          :loading="savingUser"
          @click="saveUserScope"
        >
          保存
        </el-button>
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

/* 首屏骨架：与真实布局同形 */
.scope-skeleton { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }
.sk-tile { height: 104px; border-radius: var(--au-r-lg); }
.sk-wide { grid-column: 1 / -1; height: 280px; border-radius: var(--au-r-lg); }

.scope-body { display: flex; flex-direction: column; gap: 12px; }

.ls-hint { display: block; margin: 0; font-size: 12.5px; line-height: 1.8; color: var(--au-text-3); }
.ls-hint strong { color: var(--au-text-2); }
span.ls-hint { margin-top: 6px; font-size: 12px; } /* 对话框里跟在控件下面的提示 */
.ls-switch-row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }

.ls-pick { display: flex; flex-direction: column; gap: 10px; }
.ls-pick-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.f-search { width: 220px; }

.ls-pick-list {
  max-height: 280px;
  overflow-y: auto;
  padding: 4px 12px;
  background: var(--au-surface-2);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-md);
}
.ls-dialog-list { max-height: 300px; width: 100%; }
.ls-pick-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  min-width: 0;
  padding: 6px 0;
}
.ls-pick-item + .ls-pick-item { border-top: 1px solid var(--au-border); }
.ls-lib-name { font-size: 13px; color: var(--au-text); word-break: break-all; }
.ls-lib-meta { display: flex; align-items: center; gap: 6px; flex: 0 0 auto; }
.ls-count { font-size: 11.5px; font-variant-numeric: tabular-nums; color: var(--au-text-4); }
.ls-empty { padding: 10px 4px; font-size: 12px; line-height: 1.7; color: var(--au-text-3); }

.ls-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.ls-actions-hint { flex: 1 1 260px; font-size: 12px; line-height: 1.6; color: var(--au-text-3); }
.ls-actions-hint strong { color: var(--au-text-2); }

.flush-table :deep(.dt-cards) { padding: 12px 12px 8px; }
.ls-row-actions { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.ls-user { margin-right: 6px; font-weight: 600; color: var(--au-text); }
.faint { color: var(--au-text-4); }
.badge-warn { background: var(--au-warning-soft); color: var(--au-warning); border-color: var(--au-warning-border); }

/* 手机：筛选条与按钮竖排，勾选列表占满整行 */
@media (max-width: 768px) {
  .ls-pick-head > .f-search { flex: 1 1 100%; width: auto; }
  .ls-pick-head > .el-button { flex: 1 1 100%; }
  .ls-actions > .el-button { flex: 1 1 auto; }
  .ls-actions-hint { flex: 1 1 100%; }
  .ls-pick-list { max-height: 320px; }
}
</style>
