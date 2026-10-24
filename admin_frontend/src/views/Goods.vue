<script setup lang="ts">
/**
 * 商品管理：订阅套餐 CRUD + 充值套餐 CRUD
 *
 * v2.6.20：订阅套餐是**一个服一个**的（买哪份就开哪个服的会员），所以套餐表里显示归属服，
 * 新建 / 编辑时也能改归属（把套餐从一个服搬到另一个服）。积分充值套餐是全服共用的。
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { toAdminError } from '@/utils/errorMessage'
import type { FormInstance, FormRules } from 'element-plus'
import { Coins, Crown, Plus, RefreshCw } from 'lucide-vue-next'
import { PageHeader, SectionCard } from '@/components/ui'
import DataTable from '@/components/DataTable.vue'
import type { DataColumn } from '@/components/DataTable.vue'
import { useRealmStore } from '@/stores/realm'

const realm = useRealmStore()
/** 套餐统计范围：当前服（默认）或全部服 */
const scope = ref<'realm' | 'all'>('realm')

const planColumns: DataColumn[] = [
  { key: 'name', label: '名称', width: 170, mobile: 'title' },
  { key: 'realm_name', label: '归属服', width: 130 },
  { key: 'description', label: '描述', minWidth: 170, mobile: 'hide' },
  { key: 'price', label: '价格', width: 100 },
  { key: 'points_price', label: '积分价', width: 110 },
  { key: 'duration_days', label: '时长', width: 90 },
  { key: 'is_popular', label: '推荐', width: 80, mobile: 'hide' },
  { key: 'is_active', label: '状态', width: 90 },
  { key: 'sort_order', label: '排序', width: 80, mobile: 'hide' },
  { key: 'actions', label: '操作', width: 150, fixed: 'right', align: 'right' },
]

const pkgColumns: DataColumn[] = [
  { key: 'name', label: '名称', width: 170, mobile: 'title' },
  { key: 'amount', label: '积分', width: 110 },
  { key: 'total', label: '合计', width: 100 },
  { key: 'price', label: '价格', width: 100 },
  { key: 'is_popular', label: '推荐', width: 80, mobile: 'hide' },
  { key: 'is_active', label: '状态', width: 90 },
  { key: 'sort_order', label: '排序', width: 80, mobile: 'hide' },
  { key: 'actions', label: '操作', width: 150, fixed: 'right', align: 'right' },
]
import {
  fetchEconomyPlans,
  createEconomyPlan,
  updateEconomyPlan,
  deleteEconomyPlan,
  fetchEconomyPackages,
  createEconomyPackage,
  updateEconomyPackage,
  deleteEconomyPackage,
  type PlanRowFull,
  type PackageRow,
} from '@/api/economy'

const loading = ref(false)
const loadError = ref('')
const plans = ref<PlanRowFull[]>([])
const packages = ref<PackageRow[]>([])

// ===== 套餐表单 =====
const planVisible = ref(false)
const planEditing = ref<PlanRowFull | null>(null)
const planFormRef = ref<FormInstance>()
const planForm = ref({
  name: '', description: '', price: 19.9, duration_days: 30,
  features: '', is_active: true, is_popular: false, sort_order: 0,
  points_price: null as number | null,
  realm_id: null as number | null,
  // 订阅分档权益（留空 = 不声明，跟随全站默认）；键与区间以后端
  // backend/entitlements.py 的注册表为准，保存时服务端 validate 是最终裁判
  ent_seek: null as number | null,
  ent_concurrent: null as number | null,
})

/** 权益数字校验：留空放行，填了必须是区间内整数（与后端注册表同口径） */
function entRangeRule(min: number, max: number, label: string) {
  return {
    validator: (_rule: unknown, value: number | null, callback: (e?: Error) => void) => {
      if (value == null || (Number.isInteger(value) && value >= min && value <= max)) {
        callback()
        return
      }
      callback(new Error(`${label}需在 ${min}–${max} 之间，或留空跟随全站默认`))
    },
    trigger: 'blur',
  }
}

/** 权益组装：两项全空 → null（不声明）；填了的才进对象 */
function buildPlanEntitlements(f: { ent_seek: number | null; ent_concurrent: number | null }): Record<string, number> | null {
  const out: Record<string, number> = {}
  if (f.ent_seek != null) out.media_seek_monthly_limit = f.ent_seek
  if (f.ent_concurrent != null) out.concurrent_streams = f.ent_concurrent
  return Object.keys(out).length ? out : null
}
const planRules: FormRules = {
  name: [
    { required: true, message: '请输入套餐名称', trigger: 'blur' },
    { max: 50, message: '名称最多 50 个字', trigger: 'blur' },
  ],
  realm_id: [{ required: true, message: '请选择归属服', trigger: 'change' }],
  price: [
    { required: true, message: '请输入价格', trigger: 'blur' },
    { type: 'number', min: 0, message: '价格不能为负数', trigger: 'blur' },
  ],
  duration_days: [
    { required: true, message: '请输入时长', trigger: 'blur' },
    { type: 'number', min: 1, max: 3650, message: '时长需在 1–3650 天之间', trigger: 'blur' },
  ],
  sort_order: [{ type: 'number', min: 0, message: '排序不能为负数', trigger: 'blur' }],
  ent_seek: [entRangeRule(1, 1000, '每月求片上限')],
  ent_concurrent: [entRangeRule(1, 20, '可同时播放路数')],
}

/** 校验失败时滚动到第一个错误项 */
function scrollToFirstError() {
  requestAnimationFrame(() => {
    document.querySelector('.el-dialog .el-form-item.is-error')?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  })
}

/** 可选的归属服：默认取面板当前服 */
const realmOptions = computed(() => realm.realms)

function openPlanCreate() {
  planEditing.value = null
  planForm.value = {
    name: '', description: '', price: 19.9, duration_days: 30, features: '',
    is_active: true, is_popular: false, sort_order: 0,
    points_price: null,
    realm_id: realm.activeId,
    ent_seek: null,
    ent_concurrent: null,
  }
  planVisible.value = true
}

function openPlanEdit(row: PlanRowFull) {
  planEditing.value = row
  planForm.value = {
    name: row.name,
    description: row.description || '',
    price: row.price,
    points_price: row.points_price ?? null,
    duration_days: row.duration_days,
    features: (row.features || []).join('\n'),
    is_active: row.is_active,
    is_popular: row.is_popular,
    sort_order: row.sort_order,
    realm_id: row.realm_id ?? realm.activeId,
    // 权益回填：按存量 entitlements 逐键填，缺键留空（= 不声明）
    ent_seek: row.entitlements?.media_seek_monthly_limit ?? null,
    ent_concurrent: row.entitlements?.concurrent_streams ?? null,
  }
  planVisible.value = true
}

async function savePlan() {
  try {
    await planFormRef.value?.validate()
  } catch {
    scrollToFirstError()
    return
  }
  const f = planForm.value
  const payload = {
    name: f.name.trim(),
    description: f.description.trim() || null,
    price: f.price,
    points_price: f.points_price ?? null,
    duration_days: f.duration_days,
    features: f.features.split('\n').map(s => s.trim()).filter(Boolean),
    entitlements: buildPlanEntitlements(f),
    is_active: f.is_active,
    is_popular: f.is_popular,
    sort_order: f.sort_order,
    // 一个服一个：留空时后端归到面板当前服
    realm_id: f.realm_id ?? undefined,
  }
  try {
    if (planEditing.value) {
      await updateEconomyPlan(planEditing.value.id, payload)
      ElMessage.success('套餐已更新')
    } else {
      await createEconomyPlan(payload)
      ElMessage.success('套餐已创建')
    }
    planVisible.value = false
    load()
  } catch (e: unknown) {
    ElMessage.error(toAdminError(e, '保存失败'))
  }
}

async function removePlan(row: PlanRowFull) {
  try {
    await ElMessageBox.confirm(`确认删除套餐「${row.name}」？已有订单引用时会自动改为停用。`, '删除套餐', { type: 'warning' })
  } catch {
    return // 点了取消
  }
  try {
    const res = await deleteEconomyPlan(row.id)
    ElMessage.success(res.message || '已删除')
    load()
  } catch (e: unknown) {
    ElMessage.error(toAdminError(e, '删除失败'))
  }
}

// ===== 充值套餐表单 =====
const pkgVisible = ref(false)
const pkgEditing = ref<PackageRow | null>(null)
const pkgFormRef = ref<FormInstance>()
const pkgForm = ref({ name: '', amount: 500, price: 25, bonus: 50, is_active: true, is_popular: false, sort_order: 0 })
const pkgRules: FormRules = {
  name: [
    { required: true, message: '请输入套餐名称', trigger: 'blur' },
    { max: 50, message: '名称最多 50 个字', trigger: 'blur' },
  ],
  amount: [
    { required: true, message: '请输入积分数', trigger: 'blur' },
    { type: 'number', min: 1, message: '积分数至少为 1', trigger: 'blur' },
  ],
  bonus: [{ type: 'number', min: 0, message: '赠送积分不能为负数', trigger: 'blur' }],
  price: [
    { required: true, message: '请输入价格', trigger: 'blur' },
    { type: 'number', min: 0, message: '价格不能为负数', trigger: 'blur' },
  ],
  sort_order: [{ type: 'number', min: 0, message: '排序不能为负数', trigger: 'blur' }],
}

function openPkgCreate() {
  pkgEditing.value = null
  pkgForm.value = { name: '', amount: 500, price: 25, bonus: 50, is_active: true, is_popular: false, sort_order: 0 }
  pkgVisible.value = true
}

function openPkgEdit(row: PackageRow) {
  pkgEditing.value = row
  pkgForm.value = {
    name: row.name, amount: row.amount, price: row.price, bonus: row.bonus,
    is_active: row.is_active, is_popular: row.is_popular, sort_order: row.sort_order,
  }
  pkgVisible.value = true
}

async function savePkg() {
  try {
    await pkgFormRef.value?.validate()
  } catch {
    scrollToFirstError()
    return
  }
  const f = pkgForm.value
  const payload = { ...f, name: f.name.trim() }
  try {
    if (pkgEditing.value) {
      await updateEconomyPackage(pkgEditing.value.id, payload)
      ElMessage.success('充值套餐已更新')
    } else {
      await createEconomyPackage(payload)
      ElMessage.success('充值套餐已创建')
    }
    pkgVisible.value = false
    load()
  } catch (e: unknown) {
    ElMessage.error(toAdminError(e, '保存失败'))
  }
}

async function removePkg(row: PackageRow) {
  try {
    await ElMessageBox.confirm(`确认删除充值套餐「${row.name}」？`, '删除', { type: 'warning' })
  } catch {
    return // 点了取消
  }
  try {
    const res = await deleteEconomyPackage(row.id)
    ElMessage.success(res.message || '已删除')
    load()
  } catch (e: unknown) {
    ElMessage.error(toAdminError(e, '删除失败'))
  }
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const [p, k] = await Promise.all([
      fetchEconomyPlans(scope.value === 'all' ? 0 : undefined),
      fetchEconomyPackages(),
    ])
    plans.value = p.plans
    packages.value = k.packages
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : '加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="admin-page">
    <PageHeader
      eyebrow="运营中心"
      title="订阅套餐"
      description="订阅套餐一个服一份（在哪个服买就开哪个服的会员）；积分充值套餐全服共用。"
    >
      <template #actions>
        <el-button :icon="RefreshCw" :loading="loading" @click="load">刷新</el-button>
      </template>
    </PageHeader>

    <!-- 订阅套餐 -->
    <SectionCard title="订阅套餐" :icon="Crown" :meta="loading ? '' : `${plans.length} 个`" flush>
      <template #actions>
        <el-radio-group v-model="scope" size="small" aria-label="套餐范围" @change="load">
          <el-radio-button value="realm">当前服</el-radio-button>
          <el-radio-button value="all">全部服</el-radio-button>
        </el-radio-group>
        <el-button type="primary" :icon="Plus" @click="openPlanCreate">新增套餐</el-button>
      </template>
      <DataTable
        :rows="plans"
        :columns="planColumns"
        :loading="loading"
        :error="loadError"
        empty="还没有订阅套餐"
        :empty-description="scope === 'realm' ? '当前服还没有套餐；点「新增套餐」，或切到「全部服」看看。' : '点「新增套餐」创建第一个。'"
        @retry="load"
      >
        <template #cell-name="{ row }">
          <span class="item-name">{{ row.name }}</span>
        </template>

        <template #cell-realm_name="{ row }">
          <span class="au-badge" :class="row.realm_name ? 'au-badge-muted' : 'au-badge-amber'">
            {{ row.realm_name || '未标注' }}
          </span>
        </template>

        <template #cell-description="{ row }">
          <span v-if="!row.description" class="muted">—</span>
          <span v-else>{{ row.description }}</span>
        </template>

        <template #cell-price="{ row }">
          <span class="price">¥ {{ Number(row.price).toFixed(2) }}</span>
        </template>

        <template #cell-duration_days="{ row }">{{ row.duration_days }} 天</template>

        <template #cell-is_popular="{ row }">
          <span v-if="row.is_popular" class="au-badge au-badge-amber">推荐</span>
          <span v-else class="muted">—</span>
        </template>

        <template #cell-is_active="{ row }">
          <span class="au-badge" :class="row.is_active ? 'au-badge-green' : 'au-badge-muted'">
            {{ row.is_active ? '上架' : '下架' }}
          </span>
        </template>

        <template #cell-sort_order="{ row }"><span class="mono">{{ row.sort_order }}</span></template>

        <template #cell-actions="{ row }">
          <el-button size="small" @click="openPlanEdit(row)">编辑</el-button>
          <el-button size="small" type="danger" @click="removePlan(row)">删除</el-button>
        </template>
      </DataTable>
    </SectionCard>

    <!-- 充值套餐 -->
    <SectionCard title="积分充值套餐" :icon="Coins" :meta="loading ? '全服共用' : `全服共用 · ${packages.length} 个`" flush>
      <template #actions>
        <el-button type="primary" :icon="Plus" @click="openPkgCreate">新增套餐</el-button>
      </template>
      <DataTable
        :rows="packages"
        :columns="pkgColumns"
        :loading="loading"
        :error="loadError"
        empty="还没有充值套餐"
        empty-description="用户用充值套餐购买积分，再拿积分兑换订阅或其它权益。"
        @retry="load"
      >
        <template #cell-name="{ row }">
          <span class="item-name">{{ row.name }}</span>
        </template>

        <template #cell-amount="{ row }"><span class="mono">{{ row.amount }} + {{ row.bonus }}</span></template>

        <template #cell-total="{ row }">
          <span class="price">{{ row.amount + row.bonus }}</span>
        </template>

        <template #cell-price="{ row }">
          <span class="price">¥ {{ Number(row.price).toFixed(2) }}</span>
        </template>

        <template #cell-is_popular="{ row }">
          <span v-if="row.is_popular" class="au-badge au-badge-amber">超值</span>
          <span v-else class="muted">—</span>
        </template>

        <template #cell-is_active="{ row }">
          <span class="au-badge" :class="row.is_active ? 'au-badge-green' : 'au-badge-muted'">
            {{ row.is_active ? '上架' : '下架' }}
          </span>
        </template>

        <template #cell-sort_order="{ row }"><span class="mono">{{ row.sort_order }}</span></template>

        <template #cell-actions="{ row }">
          <el-button size="small" @click="openPkgEdit(row)">编辑</el-button>
          <el-button size="small" type="danger" @click="removePkg(row)">删除</el-button>
        </template>
      </DataTable>
    </SectionCard>

    <!-- 订阅套餐对话框 -->
    <el-dialog v-model="planVisible" :title="planEditing ? '编辑订阅套餐' : '新增订阅套餐'" width="min(520px, 92vw)">
      <el-form ref="planFormRef" :model="planForm" :rules="planRules" label-position="top">
        <el-form-item label="名称" prop="name"><el-input v-model="planForm.name" maxlength="50" /></el-form-item>
        <el-form-item label="归属服" prop="realm_id">
          <el-select v-model="planForm.realm_id" placeholder="选择这个套餐属于哪个服" class="w-full">
            <el-option v-for="r in realmOptions" :key="r.id" :label="r.name" :value="r.id" />
          </el-select>
          <p class="field-help">
            用户在哪个服买这份套餐，会员就开在那个服（不同服的会员互不影响）。
          </p>
        </el-form-item>
        <el-form-item label="描述" prop="description"><el-input v-model="planForm.description" maxlength="200" /></el-form-item>
        <div class="form-grid">
          <el-form-item label="价格 (¥)" prop="price"><el-input-number v-model="planForm.price" :min="0" :precision="2" class="w-full" /></el-form-item>
          <el-form-item label="积分价" prop="points_price">
            <el-input-number v-model="planForm.points_price" :min="0" :precision="0" placeholder="空=不支持积分购买" class="w-full" />
          </el-form-item>
          <el-form-item label="时长 (天)" prop="duration_days"><el-input-number v-model="planForm.duration_days" :min="1" :max="3650" class="w-full" /></el-form-item>
        </div>
        <el-form-item label="特性" prop="features">
          <el-input v-model="planForm.features" type="textarea" :rows="3" placeholder="每行一条，如：4K 原画" />
        </el-form-item>
        <div class="form-grid">
          <el-form-item label="每月求片上限" prop="ent_seek">
            <el-input-number v-model="planForm.ent_seek" :min="1" :max="1000" placeholder="留空 = 跟随全站默认" class="w-full" />
          </el-form-item>
          <el-form-item label="可同时播放路数" prop="ent_concurrent">
            <el-input-number v-model="planForm.ent_concurrent" :min="1" :max="20" placeholder="留空 = 跟随全站默认" class="w-full" />
          </el-form-item>
        </div>
        <p class="field-help">
          权益：只对买了这个套餐的用户生效，商店里的权益文案会按这里的值自动生成；两项都留空 = 不做分档，跟随全站默认。同时播放路数还需在「防共享」里把处置档位切到记录/拦截才会真正限路。
        </p>
        <el-form-item label="排序" prop="sort_order"><el-input-number v-model="planForm.sort_order" :min="0" class="w-full" /></el-form-item>
        <el-form-item label="选项">
          <el-checkbox v-model="planForm.is_active">上架</el-checkbox>
          <el-checkbox v-model="planForm.is_popular">推荐</el-checkbox>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="planVisible = false">取消</el-button>
        <el-button type="primary" @click="savePlan">保存</el-button>
      </template>
    </el-dialog>

    <!-- 充值套餐对话框 -->
    <el-dialog v-model="pkgVisible" :title="pkgEditing ? '编辑充值套餐' : '新增充值套餐'" width="min(480px, 92vw)">
      <el-form ref="pkgFormRef" :model="pkgForm" :rules="pkgRules" label-position="top">
        <el-form-item label="名称" prop="name"><el-input v-model="pkgForm.name" maxlength="50" /></el-form-item>
        <div class="form-grid">
          <el-form-item label="积分数" prop="amount"><el-input-number v-model="pkgForm.amount" :min="1" class="w-full" /></el-form-item>
          <el-form-item label="赠送积分" prop="bonus"><el-input-number v-model="pkgForm.bonus" :min="0" class="w-full" /></el-form-item>
        </div>
        <el-form-item label="价格 (¥)" prop="price"><el-input-number v-model="pkgForm.price" :min="0" :precision="2" class="w-full" /></el-form-item>
        <el-form-item label="排序" prop="sort_order"><el-input-number v-model="pkgForm.sort_order" :min="0" class="w-full" /></el-form-item>
        <el-form-item label="选项">
          <el-checkbox v-model="pkgForm.is_active">上架</el-checkbox>
          <el-checkbox v-model="pkgForm.is_popular">超值推荐</el-checkbox>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="pkgVisible = false">取消</el-button>
        <el-button type="primary" @click="savePkg">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.item-name { font-weight: 600; color: var(--au-text); }
.price { font-weight: 600; font-variant-numeric: tabular-nums; color: var(--au-text); }
.muted { color: var(--au-text-4); }
.w-full { width: 100%; }

.form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 12px; }
.field-help { color: var(--au-text-3); font-size: 12px; margin: 5px 0 0; line-height: 1.6; }

@media (max-width: 640px) {
  .form-grid { grid-template-columns: 1fr; }
}
</style>
