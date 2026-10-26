<template>
  <div>
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:16px">
      <h2 style="margin:0">合集管理</h2>
      <el-button type="primary" @click="openCreate">新建合集</el-button>
      <el-button @click="loadSets">刷新</el-button>
    </div>

    <!-- 总闸关时：读口 404，前端明示开关名，不报成加载失败 -->
    <el-alert
      v-if="gateOff"
      title="合集功能未开启（COLLECTIONS_ENABLED）"
      description="先把合集总闸打开，本页才有内容；用户端货架读口同样会 404。"
      type="warning"
      show-icon
      style="margin-bottom:16px"
    />
    <el-alert
      v-else-if="loadError"
      :title="`合集列表加载失败：${loadError}`"
      type="error"
      show-icon
      style="margin-bottom:16px"
    />

    <el-tabs v-model="activeTab">
      <!-- ================= 合集 ================= -->
      <el-tab-pane label="合集" name="sets">
        <el-table v-loading="setsLoading" :data="sets" style="width: 100%">
          <el-table-column prop="name" label="名称" min-width="160" />
          <el-table-column prop="source_type" label="来源" width="130">
            <template #default="{ row }">{{ sourceLabel(row.source_type) }}</template>
          </el-table-column>
          <el-table-column label="可见性" width="110">
            <template #default="{ row }">
              <!-- 独立可见性开关：visible 上架、hidden 按不存在处理 -->
              <el-switch
                :model-value="row.visibility_status === 'visible'"
                active-text="可见"
                inactive-text="隐藏"
                @change="() => toggleVisibility(row, row.visibility_status !== 'visible')"
              />
            </template>
          </el-table-column>
          <el-table-column label="顺序" width="110">
            <template #default="{ row }">
              <el-input-number
                v-model="row.sort_order"
                :min="0"
                size="small"
                style="width: 90px"
                @change="orderDirty = true"
              />
            </template>
          </el-table-column>
          <el-table-column label="成员" width="150">
            <template #default="{ row }">{{ row.effective_count }} / {{ row.member_count }} 有效</template>
          </el-table-column>
          <el-table-column label="上架" width="90">
            <template #default="{ row }">
              <el-tag :type="row.shelf_eligible ? 'success' : 'info'">
                {{ row.shelf_eligible ? '可上架' : '未达门槛' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="260">
            <template #default="{ row }">
              <el-button size="small" @click="openMembers(row)">成员管理</el-button>
              <el-button size="small" @click="openEdit(row)">编辑</el-button>
              <el-button size="small" @click="refreshSet(row)">刷新</el-button>
              <el-button size="small" type="danger" @click="removeSet(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
        <div style="margin-top:12px">
          <el-button type="primary" :disabled="!orderDirty" @click="saveOrder">保存顺序</el-button>
          <span style="margin-left:8px;color:#909399;font-size:12px">改动“顺序”列后点保存顺序，整表一次提交（后端 /media-sets/order）。</span>
        </div>
      </el-tab-pane>

      <!-- ================= 模板（机制位，不预置内容） ================= -->
      <el-tab-pane label="模板" name="templates">
        <div style="margin-bottom:12px">
          <el-button type="primary" @click="openCreateTemplate">新建模板</el-button>
          <span style="margin-left:8px;color:#909399;font-size:12px">模板只存规则 JSON（由服主填），不预置任何片单；豆列导入闭环不在本页做。</span>
        </div>
        <el-table v-loading="templatesLoading" :data="templates" style="width: 100%">
          <el-table-column prop="name" label="模板名" min-width="160" />
          <el-table-column label="规则数" width="100">
            <template #default="{ row }">{{ row.rules.length }}</template>
          </el-table-column>
          <el-table-column label="操作" width="180">
            <template #default="{ row }">
              <el-button size="small" type="primary" @click="openInstantiate(row)">用模板建合集</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>
    </el-tabs>

    <!-- 新建 / 编辑合集 -->
    <el-dialog v-model="editDialog" :title="editTarget ? '编辑合集' : '新建合集'" width="520px">
      <el-form :model="editForm" label-width="110px">
        <el-form-item label="名称" required>
          <el-input v-model="editForm.name" placeholder="合集名称" />
        </el-form-item>
        <el-form-item label="来源">
          <el-select v-model="editForm.source_type" style="width:100%">
            <el-option v-for="s in SOURCE_TYPES" :key="s" :label="sourceLabel(s)" :value="s" />
          </el-select>
        </el-form-item>
        <el-form-item label="可见性">
          <el-select v-model="editForm.visibility_status" style="width:100%">
            <el-option label="可见（上架）" value="visible" />
            <el-option label="隐藏（按不存在处理）" value="hidden" />
          </el-select>
        </el-form-item>
        <el-form-item label="顺序">
          <el-input-number v-model="editForm.sort_order" :min="0" style="width:100%" />
        </el-form-item>
        <el-form-item label="最少有效成员">
          <el-input-number v-model="editForm.min_effective_members" :min="1" style="width:100%" />
        </el-form-item>
        <el-form-item label="封面策略">
          <el-select v-model="editForm.artwork_mode" style="width:100%">
            <el-option v-for="m in ARTWORK_MODES" :key="m" :label="artworkLabel(m)" :value="m" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="editForm.source_type === 'path'" label="路径前缀" required>
          <el-input v-model="editForm.path_prefix" placeholder="path 合集必填，如 /movies/" />
        </el-form-item>
        <el-form-item v-if="['rule', 'path', 'preset'].includes(editForm.source_type)" label="绑定人物 id">
          <el-input-number v-model="editForm.bound_person_id" :min="1" placeholder="仅 rule/path/preset 有效" style="width:100%" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editDialog = false">取消</el-button>
        <el-button type="primary" @click="saveEdit">保存</el-button>
      </template>
    </el-dialog>

    <!-- 成员管理 -->
    <el-dialog v-model="memberDialog" :title="`成员管理：${memberSet?.name ?? ''}`" width="640px">
      <p v-if="memberDetail" style="margin:0 0 12px;color:#909399;font-size:13px">
        规则 {{ memberDetail.rules.length }} 条（本页只读，不做规则编辑）·
        成员 {{ memberDetail.members.length }} 条 · 未绑定成员不上用户端货架（案 A 门控），成员资格本身不看绑定。
      </p>
      <el-table v-loading="memberLoading" :data="memberRows" style="width: 100%">
        <el-table-column label="#" width="50">
          <template #default="{ $index }">{{ $index + 1 }}</template>
        </el-table-column>
        <el-table-column prop="item_id" label="条目 id" width="110" />
        <el-table-column prop="member_source" label="来源" width="110" />
        <el-table-column prop="order_index" label="顺序键" width="90" />
        <el-table-column label="操作">
          <template #default="{ $index, row }">
            <el-button size="small" :disabled="$index === 0" @click="moveMember($index, -1)">上移</el-button>
            <el-button size="small" :disabled="$index === memberRows.length - 1" @click="moveMember($index, 1)">下移</el-button>
            <el-button size="small" type="danger" @click="dropMember(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div style="display:flex;gap:8px;margin-top:12px">
        <el-input-number v-model="newMemberItemId" :min="1" placeholder="条目 id" style="width:160px" />
        <el-input-number v-model="newMemberOrder" :min="0" placeholder="顺序键" style="width:120px" />
        <el-button type="primary" @click="addMember">添加成员</el-button>
        <el-button type="success" :disabled="!memberOrderDirty" @click="saveMemberOrder">保存成员顺序</el-button>
      </div>
      <p style="color:#909399;font-size:12px">上移/下移只改本地顺序，点“保存成员顺序”才一次提交（后端 /members/order）。条目 id 请从条目详情里取。顺序键 0 = 未编排：观影轨上未编排行排在已编排行之后（按上映序），所以重排从 1 开始编号，不用 0。</p>
    </el-dialog>

    <!-- 新建模板 -->
    <el-dialog v-model="templateDialog" title="新建模板" width="520px">
      <el-form :model="templateForm" label-width="90px">
        <el-form-item label="模板名" required>
          <el-input v-model="templateForm.name" placeholder="模板名称" />
        </el-form-item>
        <el-form-item label="规则 JSON">
          <el-input
            v-model="templateForm.rulesJson"
            type="textarea"
            :rows="6"
            placeholder='[{"rule_kind": "...", "value_text": "..."}]，空着表示无规则'
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="templateDialog = false">取消</el-button>
        <el-button type="primary" @click="saveTemplate">保存</el-button>
      </template>
    </el-dialog>

    <!-- 用模板建合集 -->
    <el-dialog v-model="instantiateDialog" :title="`用模板建合集：${instantiateTarget?.name ?? ''}`" width="480px">
      <el-form :model="instantiateForm" label-width="110px">
        <el-form-item label="新合集名">
          <el-input v-model="instantiateForm.name" placeholder="留空则用模板名" />
        </el-form-item>
        <el-form-item label="最少有效成员">
          <el-input-number v-model="instantiateForm.min_effective_members" :min="1" style="width:100%" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="instantiateDialog = false">取消</el-button>
        <el-button type="primary" @click="doInstantiate">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  ARTWORK_MODES,
  SOURCE_TYPES,
  collectionsAdminApi,
  type MediaSet,
  type MediaSetDetail,
  type MediaSetMember,
  type MediaSetTemplate,
} from '@/api/collections'

const activeTab = ref('sets')

const sets = ref<MediaSet[]>([])
const setsLoading = ref(false)
const loadError = ref('')
const gateOff = ref(false)
const orderDirty = ref(false)

const templates = ref<MediaSetTemplate[]>([])
const templatesLoading = ref(false)

function errText(err: unknown): string {
  const msg = (err as { userMessage?: string })?.userMessage
  if (msg) return msg
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (err instanceof Error) return err.message
  return '请求失败'
}

function isGateOff(err: unknown): boolean {
  return (err as { response?: { status?: number } })?.response?.status === 404
}

function sourceLabel(s: string): string {
  const map: Record<string, string> = {
    manual: '手工',
    rule: '规则',
    path: '路径',
    preset: '预置',
    douban_doulist: '豆列',
  }
  return map[s] ?? s
}

function artworkLabel(m: string): string {
  // 后端只出 artwork_mode 策略，不出图 URL：卡片无封面时用占位，不另起封面机制
  return m === 'auto_collage' ? '自动拼贴（无封面时占位）' : m === 'none' ? '无封面' : m
}

async function loadSets() {
  setsLoading.value = true
  loadError.value = ''
  gateOff.value = false
  try {
    sets.value = await collectionsAdminApi.list()
    orderDirty.value = false
  } catch (err: unknown) {
    if (isGateOff(err)) {
      gateOff.value = true
      sets.value = []
    } else {
      loadError.value = errText(err)
    }
  } finally {
    setsLoading.value = false
  }
}

async function loadTemplates() {
  templatesLoading.value = true
  try {
    templates.value = await collectionsAdminApi.listTemplates()
  } catch (err: unknown) {
    if (!isGateOff(err)) ElMessage.error(errText(err))
  } finally {
    templatesLoading.value = false
  }
}

async function toggleVisibility(row: MediaSet, visible: boolean) {
  const target = visible ? 'visible' : 'hidden'
  const prev = row.visibility_status
  row.visibility_status = target
  try {
    await collectionsAdminApi.patch(row.id, { visibility_status: target })
  } catch (err: unknown) {
    row.visibility_status = prev
    ElMessage.error(errText(err))
  }
}

async function saveOrder() {
  try {
    await collectionsAdminApi.order(sets.value.map((s) => ({ id: s.id, sort_order: s.sort_order })))
    orderDirty.value = false
    await loadSets()
  } catch (err: unknown) {
    ElMessage.error(errText(err))
  }
}

async function refreshSet(row: MediaSet) {
  try {
    await collectionsAdminApi.refresh(row.id)
    await loadSets()
  } catch (err: unknown) {
    ElMessage.error(errText(err))
  }
}

async function removeSet(row: MediaSet) {
  try {
    await ElMessageBox.confirm(`确定删除合集“${row.name}”吗？成员行随合集删除。`, '删除合集', { type: 'warning' })
  } catch {
    return
  }
  try {
    await collectionsAdminApi.remove(row.id)
    await loadSets()
  } catch (err: unknown) {
    ElMessage.error(errText(err))
  }
}

// ---------------- 新建 / 编辑 ----------------
const editDialog = ref(false)
const editTarget = ref<MediaSet | null>(null)
const editForm = ref({
  name: '',
  source_type: 'manual',
  visibility_status: 'visible',
  sort_order: 0,
  min_effective_members: 2,
  artwork_mode: 'auto_collage',
  path_prefix: '',
  bound_person_id: null as number | null,
})

function openCreate() {
  editTarget.value = null
  editForm.value = {
    name: '',
    source_type: 'manual',
    visibility_status: 'visible',
    sort_order: 0,
    min_effective_members: 2,
    artwork_mode: 'auto_collage',
    path_prefix: '',
    bound_person_id: null,
  }
  editDialog.value = true
}

function openEdit(row: MediaSet) {
  editTarget.value = row
  editForm.value = {
    name: row.name,
    source_type: row.source_type,
    visibility_status: row.visibility_status,
    sort_order: row.sort_order,
    min_effective_members: row.min_effective_members,
    artwork_mode: row.artwork_mode,
    path_prefix: row.path_prefix ?? '',
    bound_person_id: row.bound_person_id,
  }
  editDialog.value = true
}

async function saveEdit() {
  const f = editForm.value
  if (!f.name.trim()) {
    ElMessage.error('名称不能为空')
    return
  }
  const body = {
    name: f.name.trim(),
    source_type: f.source_type,
    visibility_status: f.visibility_status,
    sort_order: f.sort_order,
    min_effective_members: f.min_effective_members,
    artwork_mode: f.artwork_mode,
    path_prefix: f.source_type === 'path' ? (f.path_prefix || null) : null,
    bound_person_id: ['rule', 'path', 'preset'].includes(f.source_type) ? f.bound_person_id : null,
  }
  try {
    if (editTarget.value) {
      await collectionsAdminApi.patch(editTarget.value.id, body)
    } else {
      await collectionsAdminApi.create(body)
    }
    editDialog.value = false
    await loadSets()
  } catch (err: unknown) {
    ElMessage.error(errText(err))
  }
}

// ---------------- 成员管理 ----------------
const memberDialog = ref(false)
const memberSet = ref<MediaSet | null>(null)
const memberDetail = ref<MediaSetDetail | null>(null)
const memberRows = ref<MediaSetMember[]>([])
const memberLoading = ref(false)
const memberOrderDirty = ref(false)
const newMemberItemId = ref<number | null>(null)
const newMemberOrder = ref(0)

async function openMembers(row: MediaSet) {
  memberSet.value = row
  memberDialog.value = true
  memberLoading.value = true
  memberOrderDirty.value = false
  newMemberItemId.value = null
  try {
    memberDetail.value = await collectionsAdminApi.get(row.id)
    memberRows.value = [...memberDetail.value.members]
    // 新成员默认排最后：已有最大顺序键 +1（0 留给“未编排”，不用它占位）
    newMemberOrder.value = memberRows.value.reduce((m, r) => Math.max(m, r.order_index), 0) + 1
  } catch (err: unknown) {
    ElMessage.error(errText(err))
    memberDialog.value = false
  } finally {
    memberLoading.value = false
  }
}

function moveMember(index: number, delta: number) {
  const j = index + delta
  if (j < 0 || j >= memberRows.value.length) return
  const rows = [...memberRows.value]
  const [moved] = rows.splice(index, 1)
  rows.splice(j, 0, moved)
  // 从 1 开始编号：0 在后端语义里是“未编排”，观影轨会把它沉到已编排行之后
  rows.forEach((r, i) => { r.order_index = i + 1 })
  memberRows.value = rows
  memberOrderDirty.value = true
}

async function saveMemberOrder() {
  if (!memberSet.value) return
  try {
    await collectionsAdminApi.orderMembers(
      memberSet.value.id,
      memberRows.value.map((r) => ({ item_id: r.item_id, order_index: r.order_index })),
    )
    memberOrderDirty.value = false
    ElMessage.success('成员顺序已保存')
  } catch (err: unknown) {
    ElMessage.error(errText(err))
  }
}

async function addMember() {
  if (!memberSet.value || !newMemberItemId.value) {
    ElMessage.error('请填写条目 id')
    return
  }
  try {
    await collectionsAdminApi.addMember(memberSet.value.id, {
      item_id: newMemberItemId.value,
      order_index: newMemberOrder.value,
    })
    newMemberItemId.value = null
    await openMembers(memberSet.value)
    await loadSets()
  } catch (err: unknown) {
    ElMessage.error(errText(err))
  }
}

async function dropMember(row: MediaSetMember) {
  if (!memberSet.value) return
  try {
    await collectionsAdminApi.removeMember(memberSet.value.id, row.item_id)
    await openMembers(memberSet.value)
    await loadSets()
  } catch (err: unknown) {
    ElMessage.error(errText(err))
  }
}

// ---------------- 模板 ----------------
const templateDialog = ref(false)
const templateForm = ref({ name: '', rulesJson: '' })
const instantiateDialog = ref(false)
const instantiateTarget = ref<MediaSetTemplate | null>(null)
const instantiateForm = ref({ name: '', min_effective_members: 2 as number | null })

function openCreateTemplate() {
  templateForm.value = { name: '', rulesJson: '' }
  templateDialog.value = true
}

async function saveTemplate() {
  if (!templateForm.value.name.trim()) {
    ElMessage.error('模板名不能为空')
    return
  }
  let rules: Record<string, unknown>[] = []
  const raw = templateForm.value.rulesJson.trim()
  if (raw) {
    try {
      const parsed: unknown = JSON.parse(raw)
      if (!Array.isArray(parsed)) throw new Error('规则 JSON 必须是数组')
      rules = parsed as Record<string, unknown>[]
    } catch {
      ElMessage.error('规则 JSON 解析失败：必须是 JSON 数组')
      return
    }
  }
  try {
    await collectionsAdminApi.createTemplate({ name: templateForm.value.name.trim(), rules })
    templateDialog.value = false
    await loadTemplates()
  } catch (err: unknown) {
    ElMessage.error(errText(err))
  }
}

function openInstantiate(t: MediaSetTemplate) {
  instantiateTarget.value = t
  instantiateForm.value = { name: '', min_effective_members: 2 }
  instantiateDialog.value = true
}

async function doInstantiate() {
  if (!instantiateTarget.value) return
  try {
    await collectionsAdminApi.instantiateTemplate(instantiateTarget.value.id, {
      name: instantiateForm.value.name.trim() || null,
      min_effective_members: instantiateForm.value.min_effective_members,
    })
    instantiateDialog.value = false
    activeTab.value = 'sets'
    await loadSets()
  } catch (err: unknown) {
    ElMessage.error(errText(err))
  }
}

onMounted(() => {
  void loadSets()
  void loadTemplates()
})
</script>
