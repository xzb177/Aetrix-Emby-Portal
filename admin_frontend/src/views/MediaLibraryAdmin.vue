<script setup lang="ts">
/**
 * 媒体库图片与顺序（媒体库学习 L2 / L4 管理端）。
 *
 * 上半：库顺序——Views 里库的展示顺序。0 = 未设序（排在已设序之后）；
 * 已设序的按数值升序在前。改完点「保存顺序」整单写后端。
 * 下半：条目图人工覆盖——管理员给条目手工指定海报 / 背景图。
 * 优先级：覆盖 > 旁挂 > 刮削。覆盖只接受 http(s) URL；读侧是否生效
 * 由 ITEM_ARTWORK_OVERRIDE_ENABLED 开关决定（写侧不受开关影响，
 * 可以先配好再开开关）。
 */
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Image as ImageIcon, ListOrdered } from 'lucide-vue-next'
import { PageHeader, SectionCard } from '@/components/ui'
import {
  deleteArtworkOverride,
  fetchArtworkOverrides,
  fetchLibraryOrder,
  setArtworkOverride,
  setLibraryOrder,
  type ArtworkOverrideRow,
  type LibraryOrderRow,
} from '@/api/admin'

const orderRows = ref<LibraryOrderRow[]>([])
const orderLoading = ref(false)
const orderSaving = ref(false)

const overrideRows = ref<ArtworkOverrideRow[]>([])
const overrideLoading = ref(false)
const formItemId = ref<number | null>(null)
const formKind = ref('Primary')
const formUrl = ref('')
const formSaving = ref(false)

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
  void loadOrder()
  void loadOverrides()
})
</script>

<template>
  <div>
    <PageHeader title="图片与顺序" description="库展示顺序与条目图人工覆盖（媒体库学习 L2 / L4）" />

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
