<script setup lang="ts">
/**
 * 媒体库图片与顺序（媒体库学习 L1 / L2 / L4 管理端）。
 *
 * 上：库封面策略——库封面按什么顺序尝试出图（借代表海报 / 自研渲染 /
 * 直传 / SVG 占位）。顺序即尝试顺序；管理端保存进 system_configs，
 * 文件配置 LIBRARY_COVER_STRATEGY_ORDER 已设置时优先于此处保存；
 * 借代表一步仅在借代表开关开启时生效（文件配置，只读）。
 * 中：库顺序——Views 里库的展示顺序。0 = 未设序（排在已设序之后）；
 * 已设序的按数值升序在前。改完点「保存顺序」整单写后端。
 * 下：条目图人工覆盖——管理员给条目手工指定海报 / 背景图。
 * 优先级：覆盖 > 旁挂 > 刮削。覆盖只接受 http(s) URL；读侧是否生效
 * 由 ITEM_ARTWORK_OVERRIDE_ENABLED 开关决定（写侧不受开关影响，
 * 可以先配好再开开关）。
 *
 * 布局纪律：本页就是媒体库设置的收纳面，封面与顺序都在此分组呈现，
 * 不另起导航入口与新页面。
 */
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { ArrowDown, ArrowUp, Image as ImageIcon, ListOrdered } from 'lucide-vue-next'
import { PageHeader, SectionCard } from '@/components/ui'
import {
  deleteArtworkOverride,
  fetchArtworkOverrides,
  fetchCoverStrategy,
  fetchLibraryOrder,
  setArtworkOverride,
  setCoverStrategy,
  setLibraryOrder,
  type ArtworkOverrideRow,
  type LibraryOrderRow,
} from '@/api/admin'

const STRATEGY_META: Record<string, { label: string; desc: string }> = {
  borrow: { label: '借代表海报', desc: '借库内已绑定代表条目的海报' },
  render: { label: '自研渲染封面', desc: '库已设置的渲染封面' },
  uploaded: { label: '管理员直传', desc: '管理员直接上传的封面' },
  svg: { label: 'SVG 占位', desc: '无图时给占位图，不破图' },
}

interface StrategyRow {
  step: string
  included: boolean
}

const strategyRows = ref<StrategyRow[]>([])
const strategyLoading = ref(false)
const strategySaving = ref(false)
const strategyEnvOrder = ref<string[] | null>(null)
const strategyBorrowEnabled = ref(false)

const orderRows = ref<LibraryOrderRow[]>([])
const orderLoading = ref(false)
const orderSaving = ref(false)

const overrideRows = ref<ArtworkOverrideRow[]>([])
const overrideLoading = ref(false)
const formItemId = ref<number | null>(null)
const formKind = ref('Primary')
const formUrl = ref('')
const formSaving = ref(false)

async function loadStrategy() {
  strategyLoading.value = true
  try {
    const d = await fetchCoverStrategy()
    strategyEnvOrder.value = d.env_order
    strategyBorrowEnabled.value = d.borrow_enabled
    // 可编辑顺序：已保存的用保存的，否则用当前生效顺序；已勾选的在前
    const editOrder = d.stored_order ?? d.effective_order
    const included = editOrder.filter((s) => d.known_steps.includes(s))
    const excluded = d.known_steps.filter((s) => !included.includes(s))
    strategyRows.value = [
      ...included.map((step) => ({ step, included: true })),
      ...excluded.map((step) => ({ step, included: false })),
    ]
  } catch {
    ElMessage.error('库封面策略加载失败')
  } finally {
    strategyLoading.value = false
  }
}

function toggleStrategy(row: StrategyRow) {
  row.included = !row.included
  // 勾选的沉到已勾选区末尾，取消的沉到未勾选区，保持两区各自有序
  const included = strategyRows.value.filter((r) => r.included)
  const excluded = strategyRows.value.filter((r) => !r.included)
  strategyRows.value = [...included, ...excluded]
}

function moveStrategy(index: number, dir: -1 | 1) {
  const rows = strategyRows.value
  const target = index + dir
  if (target < 0 || target >= rows.length) return
  if (!rows[index].included || !rows[target].included) return
  ;[rows[index], rows[target]] = [rows[target], rows[index]]
}

async function saveStrategy() {
  const order = strategyRows.value.filter((r) => r.included).map((r) => r.step)
  if (order.length === 0) {
    ElMessage.warning('至少保留一步封面策略')
    return
  }
  strategySaving.value = true
  try {
    await setCoverStrategy(order)
    ElMessage.success('库封面策略已保存')
    await loadStrategy()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '库封面策略保存失败')
  } finally {
    strategySaving.value = false
  }
}

async function loadOrder() {
  orderLoading.value = true
  try {
    const d = await fetchLibraryOrder()
    orderRows.value = d.items
  } catch {
    ElMessage.error('库顺序加载失败')
  } finally {
    orderLoading.value = false
  }
}

async function saveOrder() {
  orderSaving.value = true
  try {
    await setLibraryOrder(orderRows.value.map((r) => ({
      library_id: r.library_id,
      sort_order: r.sort_order || 0,
    })))
    ElMessage.success('库顺序已保存')
    await loadOrder()
  } catch {
    ElMessage.error('库顺序保存失败')
  } finally {
    orderSaving.value = false
  }
}

async function loadOverrides() {
  overrideLoading.value = true
  try {
    const d = await fetchArtworkOverrides()
    overrideRows.value = d.items
  } catch {
    ElMessage.error('覆盖列表加载失败')
  } finally {
    overrideLoading.value = false
  }
}

async function submitOverride() {
  if (formItemId.value == null || !formUrl.value.trim()) {
    ElMessage.warning('请填条目 id 与图片 URL')
    return
  }
  formSaving.value = true
  try {
    await setArtworkOverride({
      item_id: formItemId.value,
      kind: formKind.value,
      image_url: formUrl.value.trim(),
    })
    ElMessage.success('覆盖已保存')
    formUrl.value = ''
    await loadOverrides()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '覆盖保存失败（只接受 http(s) URL）')
  } finally {
    formSaving.value = false
  }
}

async function removeOverride(row: ArtworkOverrideRow) {
  try {
    await deleteArtworkOverride(row.item_id, row.kind)
    ElMessage.success('覆盖已取消')
    await loadOverrides()
  } catch {
    ElMessage.error('取消覆盖失败')
  }
}

onMounted(() => {
  void loadStrategy()
  void loadOrder()
  void loadOverrides()
})
</script>

<template>
  <div>
    <PageHeader title="图片与顺序" description="库封面策略、库展示顺序与条目图人工覆盖（媒体库学习 L1 / L2 / L4）" />

    <SectionCard title="库封面策略" :icon="ImageIcon">
      <template #actions>
        <el-button type="primary" :loading="strategySaving" @click="saveStrategy">保存策略</el-button>
      </template>
      <p class="ml-note">顺序即尝试顺序：有图的步骤先命中。借代表一步仅在借代表开关开启时生效，当前：{{ strategyBorrowEnabled ? '已开启' : '未开启（文件配置）' }}。文件配置已设置时优先于此处保存，当前：{{ strategyEnvOrder ? '已设置' : '未设置' }}。保存后下次出图即生效。</p>
      <el-table v-loading="strategyLoading" :data="strategyRows" style="width: 100%">
        <el-table-column label="启用" width="70">
          <template #default="{ row }">
            <el-checkbox :model-value="row.included" @change="toggleStrategy(row)" />
          </template>
        </el-table-column>
        <el-table-column label="步骤">
          <template #default="{ row }">
            {{ STRATEGY_META[row.step]?.label ?? row.step }}
            <span class="ml-note">（{{ STRATEGY_META[row.step]?.desc ?? '' }}）</span>
          </template>
        </el-table-column>
        <el-table-column label="调整" width="130">
          <template #default="{ $index }">
            <el-button link :disabled="$index === 0 || !strategyRows[$index].included" @click="moveStrategy($index, -1)">
              <el-icon><ArrowUp /></el-icon>
            </el-button>
            <el-button link :disabled="$index >= strategyRows.filter((r) => r.included).length - 1 || !strategyRows[$index].included" @click="moveStrategy($index, 1)">
              <el-icon><ArrowDown /></el-icon>
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </SectionCard>

    <SectionCard title="库顺序" :icon="ListOrdered">
      <template #actions>
        <el-button type="primary" :loading="orderSaving" @click="saveOrder">保存顺序</el-button>
      </template>
      <p class="ml-note">0 = 未设序，排在已设序之后；已设序的按数值升序在前。全部为 0 时 Views 顺序与今天逐字一致。</p>
      <el-table v-loading="orderLoading" :data="orderRows" style="width: 100%">
        <el-table-column prop="name" label="媒体库" />
        <el-table-column label="顺序值" width="180">
          <template #default="{ row }">
            <el-input-number v-model="row.sort_order" :min="-9999" :max="9999" controls-position="right" />
          </template>
        </el-table-column>
      </el-table>
    </SectionCard>

    <SectionCard title="条目图覆盖" :icon="ImageIcon">
      <p class="ml-note">覆盖只接受 http(s) URL；读侧生效受 ITEM_ARTWORK_OVERRIDE_ENABLED 开关控制，关时配好的覆盖不生效、也不影响现有出图。</p>
      <el-form label-position="top" class="ml-form" @submit.prevent="submitOverride">
        <el-form-item label="条目 id（数字，见条目详情）">
          <el-input-number v-model="formItemId" :min="1" placeholder="条目 id" />
        </el-form-item>
        <el-form-item label="图类">
          <el-select v-model="formKind">
            <el-option label="海报 Primary" value="Primary" />
            <el-option label="背景 Backdrop" value="Backdrop" />
          </el-select>
        </el-form-item>
        <el-form-item label="图片 URL（http(s)）">
          <el-input v-model="formUrl" placeholder="https://…" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="formSaving" @click="submitOverride">保存覆盖</el-button>
        </el-form-item>
      </el-form>
      <el-table v-loading="overrideLoading" :data="overrideRows" style="width: 100%">
        <el-table-column prop="item_name" label="条目" />
        <el-table-column prop="item_type" label="类型" width="100" />
        <el-table-column prop="kind" label="图类" width="110" />
        <el-table-column prop="image_url" label="图片 URL" min-width="260" />
        <el-table-column label="操作" width="100">
          <template #default="{ row }">
            <el-button type="danger" link @click="removeOverride(row)">取消</el-button>
          </template>
        </el-table-column>
      </el-table>
    </SectionCard>
  </div>
</template>

<style scoped>
.ml-note { opacity: 0.75; font-size: 13px; margin: 0 0 12px; }
.ml-form { max-width: 560px; }
</style>
