<script setup lang="ts">
/**
 * 添加路径 / 浏览目录弹窗（媒体库设置的「媒体路径」区块用）
 *
 * 交互按「先说清楚存在哪，再挑目录」排：
 *   1. **默认就是服务器本地硬盘**，直接展开目录就能选（99% 的库都是它）；
 *   2. 目录在 115 / Rclone 上时，展开「高级」选**存储后端** + 对应的**存储挂载**
 *      （同一后端可能有多个挂载，如「115 影库」「115 备份」）；
 *   3. 浏览目录。本机目录与挂载目录的**列表 UI 完全一样**——两个后端接口返回同一套
 *      结构（browseLocalDirs / browseMountDirs），只有“取回路径要不要拼挂载 id”不同。
 *
 * 关键约定：这里**不出现 mount:// 前缀**，只吐 `{path, backend, mount_id}`；
 * 前缀由后端 assemble_library_path 在保存时拼回去（见 backend/emby_server/mounts.py）。
 */
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { FolderOpen, HardDrive, HardDriveDownload, Plus } from 'lucide-vue-next'
import { browseLocalDirs, browseMountDirs } from '@/api/admin'
import type { MountPickerCrumb, MountPickerDir } from '@/api/admin'
import type { LibraryPathEntry, StorageBackend, StorageMount } from '@/types'

const props = withDefaults(defineProps<{
  modelValue: boolean
  /** 可选存储挂载（只列出启用中的） */
  mounts: StorageMount[]
  /** 存储后端下拉项（后端下发） */
  backends: StorageBackend[]
  /** 编辑已有路径时传它：直接定位到当前目录，后端与挂载也预选好 */
  preset?: LibraryPathEntry | null
  /** 已有路径：用于去重与提示 */
  existing?: LibraryPathEntry[]
}>(), {
  preset: null,
  existing: () => [],
})

const emit = defineEmits<{
  'update:modelValue': [boolean]
  /** 确认：一次可返回多条（多选模式） */
  confirm: [entries: Array<{ path: string; backend: string; mount_id: number | null }>]
}>()

const BACKEND_LOCAL = 'local'

const backend = ref(BACKEND_LOCAL)
const mountId = ref<number | null>(null)
/** 「高级」折叠区默认收起：后端选本地时它根本用不上（v2.46.0） */
const advancedOpen = ref(false)
/** el-collapse 的 v-model 是「展开项名字」，所以在这里翻成数组再绑上去 */
const advancedNames = computed<string[]>({
  get: () => (advancedOpen.value ? ['advanced'] : []),
  set: (names) => { advancedOpen.value = names.includes('advanced') },
})
const multi = ref(false)
/** 多选时跨层级保留的目录（存的是展示路径，不是 mount:// 全路径） */
const checked = ref<string[]>([])
const pathInput = ref('')
const currentPath = ref('/')
const parentPath = ref<string | null>(null)
const crumbs = ref<MountPickerCrumb[]>([])
const dirs = ref<MountPickerDir[]>([])
const loading = ref(false)
const loadError = ref('')
const truncated = ref(false)

/** 本机来源才需要挂载下拉；远程来源按类型过滤出可用的挂载 */
const mountOptions = computed(() => {
  const want = backend.value.toLowerCase()
  return props.mounts.filter((m) => m.is_enabled !== false && (m.mount_type || '') === want)
})

/** 远程来源但一个挂载都没配：说清去哪儿配，别让人在一个空列表里干瞪眼 */
const missingMount = computed(() => backend.value !== BACKEND_LOCAL && mountOptions.value.length === 0)

const canBrowse = computed(() => backend.value === BACKEND_LOCAL || !!mountId.value)

const backendLabel = computed(
  () => props.backends.find((b) => b.value === backend.value)?.label || backend.value,
)

/** 改一条「前缀路径」（没绑挂载的老写法）时要说清为什么要选挂载，否则只能干瞪眼 */
const presetIsPrefix = computed(() => !!props.preset
  && props.preset.mount_id == null
  && props.preset.backend !== BACKEND_LOCAL)

/** 标题要能看出这是“换一条”还是“加一条”：弹窗开着时看不出区别会让人以为多了一条 */
const dialogTitle = computed(() => (
  props.preset ? `更换目录：${props.preset.path}` : '添加媒体路径'
))

/** 当前目录（远程来源是挂载内路径，本机来源是绝对路径） */
const currentLabel = computed(() => currentPath.value || '/')

function resetTo(entry: LibraryPathEntry | null) {
  checked.value = []
  loadError.value = ''
  if (!entry) {
    backend.value = BACKEND_LOCAL
    mountId.value = null
    pathInput.value = ''
    currentPath.value = '/'
    crumbs.value = []
    dirs.value = []
    advancedOpen.value = false
    return
  }
  backend.value = (entry.backend || BACKEND_LOCAL).toLowerCase()
  mountId.value = entry.mount_id ?? (mountOptions.value[0]?.id ?? null)
  pathInput.value = backend.value === BACKEND_LOCAL ? entry.path : ''
  currentPath.value = entry.path || '/'
  crumbs.value = []
  dirs.value = []
  // 改的本身就是远程来源时把「高级」推开：不然用户只会看到一个空的本机列表，
  // 看不出自己正在改的是 115 / Rclone 路径
  advancedOpen.value = backend.value !== BACKEND_LOCAL
}

/** 打开时：新建从「本地文件」起步；编辑则定位到它原来的位置 */
watch(() => props.modelValue, (open) => {
  if (!open) return
  resetTo(props.preset)
  multi.value = false
  // 默认把第一个可浏览的挂载选上，省掉一次点击（只有一个挂载时直接就能浏览）
  if (backend.value !== BACKEND_LOCAL && mountId.value == null) {
    mountId.value = mountOptions.value[0]?.id ?? null
  }
  if (canBrowse.value) void load('/')
})

async function onBackendChange() {
  // 换后端 = 换一套目录树，之前勾选的目录语义全变了，整批作废
  checked.value = []
  mountId.value = backend.value === BACKEND_LOCAL ? null : (mountOptions.value[0]?.id ?? null)
  // 选回本地就把「高级」收起来，否则面板会一直开着，里面却什么都没选
  advancedOpen.value = backend.value !== BACKEND_LOCAL
  if (canBrowse.value) await load('/')
}

function onMountChange() {
  checked.value = []
  void load('/')
}

/** 两种来源都吐同一套结构：{path, parent, crumbs, dirs} */
async function load(path: string) {
  if (!canBrowse.value) return
  loading.value = true
  loadError.value = ''
  truncated.value = false
  try {
    if (backend.value === BACKEND_LOCAL) {
      const res = await browseLocalDirs(path)
      currentPath.value = res.path || '/'
      parentPath.value = res.parent ?? null
      crumbs.value = res.crumbs || []
      dirs.value = res.dirs || []
      truncated.value = !!res.truncated
      pathInput.value = currentPath.value
    } else if (mountId.value) {
      const res = await browseMountDirs(mountId.value, path === '/' ? undefined : path)
      currentPath.value = res.path || '/'
      parentPath.value = res.parent ?? null
      crumbs.value = res.crumbs || []
      dirs.value = res.dirs || []
      // 地址栏始终回显当前所在目录：禁用的输入框也得让人知道自己在哪一层
      pathInput.value = currentPath.value
    }
  } catch (e: any) {
    loadError.value = e?.response?.data?.detail || e?.message || '读取目录失败'
    dirs.value = []
  } finally {
    loading.value = false
  }
}

/** 本机目录没有面包屑可点（从 / 一路点下来太远），所以给个可直接输入的地址栏 */
async function jumpToTyped() {
  const target = (pathInput.value || '').trim()
  if (!target) return
  if (backend.value !== BACKEND_LOCAL) {
    currentPath.value = target.startsWith('/') ? target : `/${target}`
    await load(currentPath.value)
    return
  }
  await load(target.startsWith('/') ? target : `/${target}`)
}

function go(path: string | null) {
  if (path === null) return
  void load(path)
}

function isChecked(path: string): boolean {
  return !!path && checked.value.includes(path)
}

function toggle(path: string) {
  const i = checked.value.indexOf(path)
  if (i >= 0) checked.value.splice(i, 1)
  else checked.value.push(path)
}

function existsAlready(path: string): boolean {
  return props.existing.some((e) => e.path === path && (e.mount_id ?? null) === mountId.value)
}

/** 把一条目录变成条目；已有的一律跳过并报数（「我选了 5 个怎么只加了 2 个」最容易被当成坏了） */
function toEntry(path: string): { path: string; backend: string; mount_id: number | null } | null {
  if (!path || existsAlready(path)) return null
  return {
    path,
    backend: backend.value,
    mount_id: backend.value === BACKEND_LOCAL ? null : mountId.value,
  }
}

function confirmCurrent() {
  const entry = toEntry(currentLabel.value)
  if (!entry) {
    ElMessage.info('这个目录已经在路径列表里了')
    return
  }
  emit('confirm', [entry])
  emit('update:modelValue', false)
}

function confirmChecked() {
  const added: Array<{ path: string; backend: string; mount_id: number | null }> = []
  let skipped = 0
  for (const path of checked.value) {
    const entry = toEntry(path)
    if (entry) added.push(entry)
    else skipped += 1
  }
  if (!added.length) {
    ElMessage.info(skipped ? `所选的 ${skipped} 个目录都已在路径列表里` : '还没勾选目录')
    return
  }
  emit('confirm', added)
  emit('update:modelValue', false)
  if (skipped) ElMessage.warning(`已添加 ${added.length} 个目录，跳过 ${skipped} 个已存在`)
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    :title="dialogTitle"
    width="min(620px, 92vw)"
    :close-on-click-modal="false"
    @update:model-value="(v: boolean) => emit('update:modelValue', v)"
  >
    <div class="lpd-body">
      <!--
        存储位置：默认就是本机硬盘，所以不再占一个下拉框置顶，改成一行回显；
        115 / Rclone 收到下面的「高级」里，需要时才展开（v2.46.0）。
      -->
      <div class="lpd-source">
        <HardDrive :size="14" class="lpd-source-icon" />
        <span>目录存在：<b>{{ backendLabel }}</b></span>
        <el-tag v-if="mountId != null" size="small" type="primary" effect="plain">
          {{ props.mounts.find((m) => m.id === mountId)?.name }}
        </el-tag>
      </div>

      <el-collapse v-model="advancedNames" class="lpd-advanced">
        <el-collapse-item title="高级：目录不在本机（115 / Rclone）" name="advanced">
          <el-form label-position="top">
            <el-form-item label="存储位置">
              <el-select v-model="backend" style="width: 100%" @change="onBackendChange">
                <el-option
                  v-for="b in backends"
                  :key="b.value"
                  :label="b.label"
                  :value="b.value"
                />
              </el-select>
              <p class="lpd-hint">
                先选这个目录存在哪：服务器硬盘、Rclone 远程，还是 115 网盘。路径只写目录本身，
                不带任何技术前缀。
              </p>
            </el-form-item>

            <el-form-item v-if="backend !== BACKEND_LOCAL" label="存储挂载">
              <el-select v-model="mountId" style="width: 100%" @change="onMountChange">
                <el-option
                  v-for="m in mountOptions"
                  :key="m.id"
                  :label="`${m.name}（${m.mount_type || ''}）`"
                  :value="m.id"
                />
              </el-select>
              <p v-if="missingMount" class="lpd-warn">
                还没有配置{{ backendLabel }}存储挂载。挂载里存着账号与连接信息，请到「服务器」页新建一个再回来。
              </p>
              <p v-else class="lpd-hint">
                同一后端可以有多个挂载（如「115 影库」「115 备份」），这里选这次要用的那一个。
              </p>
            </el-form-item>
          </el-form>
        </el-collapse-item>
      </el-collapse>

      <div class="lpd-toolbar">
        <el-input
          v-model="pathInput"
          :placeholder="backend === BACKEND_LOCAL ? '/media/电影（可直接粘贴绝对路径）' : '当前目录'"
          :disabled="backend !== BACKEND_LOCAL"
          @keyup.enter="jumpToTyped"
        >
          <template #append>
            <el-button :disabled="backend !== BACKEND_LOCAL" @click="jumpToTyped">前往</el-button>
          </template>
        </el-input>
        <el-checkbox v-model="multi" class="lpd-multi">多选</el-checkbox>
      </div>

      <div v-if="canBrowse && crumbs.length" class="lpd-crumbs">
        <el-breadcrumb separator="/">
          <el-breadcrumb-item><a @click.prevent="go('/')">根目录</a></el-breadcrumb-item>
          <el-breadcrumb-item v-for="c in crumbs" :key="c.path">
            <a @click.prevent="go(c.path)">{{ c.name }}</a>
          </el-breadcrumb-item>
        </el-breadcrumb>
      </div>

      <div v-if="canBrowse" v-loading="loading" class="lpd-list">
        <div v-if="parentPath" class="lpd-row lpd-up" @click="go(parentPath)">
          <span class="lpd-icon">↩</span> 返回上一级
        </div>
        <div
          v-for="d in dirs"
          :key="d.path"
          class="lpd-row"
          :class="{ picked: multi && isChecked(d.path) }"
          @click="go(d.path)"
        >
          <span
            v-if="multi"
            class="lpd-box"
            :class="{ on: isChecked(d.path) }"
            role="checkbox"
            :aria-checked="isChecked(d.path)"
            :aria-label="`勾选 ${d.name}`"
            @click.stop="toggle(d.path)"
          >✓</span>
          <FolderOpen :size="15" class="lpd-icon" />
          <span class="lpd-name">{{ d.name }}</span>
          <span v-if="existsAlready(d.path)" class="lpd-tag">已添加</span>
        </div>
        <div v-if="!dirs.length && !loading && !loadError" class="lpd-empty">
          目录为空{{ truncated ? '（子目录过多，只列了前一部分）' : '' }}
        </div>
        <div v-if="loadError" class="lpd-error">{{ loadError }}</div>
      </div>
      <div v-else class="lpd-empty">
        <template v-if="presetIsPrefix">
          这一条是旧写法的前缀路径（没绑挂载），改成{{ backendLabel }}挂载后就能浏览目录了。
        </template>
        <template v-else>展开上方「高级」选一个{{ backendLabel }}存储挂载，就能浏览目录了</template>
      </div>

      <div v-if="multi && checked.length" class="lpd-checked">
        已勾选 <b>{{ checked.length }}</b> 个目录
        <a class="lpd-clear" @click.prevent="checked = []">清空</a>
      </div>
      <p v-if="truncated" class="lpd-hint">
        <HardDriveDownload :size="13" style="vertical-align: -2px; margin-right: 4px" />
        子目录超过 1000 个，只列了前一部分——用上方地址栏直接输入完整路径更快。
      </p>
    </div>

    <template #footer>
      <el-button @click="emit('update:modelValue', false)">取消</el-button>
      <el-button v-if="!multi" type="primary" :disabled="!canBrowse" @click="confirmCurrent">
        <Plus :size="14" style="margin-right: 4px" />添加当前目录
      </el-button>
      <el-button v-else type="primary" :disabled="!checked.length" @click="confirmChecked">
        添加所选（{{ checked.length }}）
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.lpd-body { min-height: 220px; }
.lpd-source {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  padding: 7px 12px;
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-md);
  background: var(--au-bg-soft);
  font-size: var(--font-size-sm);
  color: var(--au-text-3);
}
.lpd-source b { color: var(--au-text); }
.lpd-source-icon { flex: none; }
.lpd-advanced { margin-bottom: 10px; }
.lpd-hint {
  margin: 4px 0 0;
  font-size: var(--font-size-xs);
  color: var(--au-text-3);
  line-height: 1.6;
}
.lpd-warn {
  margin: 4px 0 0;
  font-size: var(--font-size-xs);
  color: var(--au-warning);
  line-height: 1.6;
}
.lpd-toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.lpd-multi { flex: none; }
.lpd-crumbs { margin-bottom: 6px; font-size: var(--font-size-xs); }
.lpd-crumbs a { cursor: pointer; color: var(--au-primary); }
.lpd-crumbs a:hover { color: var(--au-primary-strong); }
.lpd-list {
  max-height: 46vh;
  min-height: 140px;
  overflow-y: auto;
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-md);
}
.lpd-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  cursor: pointer;
  font-size: var(--font-size-sm);
  border-bottom: 1px solid var(--au-border);
}
.lpd-row:last-child { border-bottom: none; }
.lpd-row { transition: background-color var(--au-fast) var(--au-ease); }
.lpd-row:hover { background: var(--au-violet-soft); }
.lpd-row.picked { background: var(--au-primary-soft); }
.lpd-up { color: var(--au-text-3); }
.lpd-icon { flex: none; color: var(--au-text-3); }
.lpd-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.lpd-tag { flex: none; font-size: var(--font-size-xs); color: var(--au-text-4); }
.lpd-box {
  flex: none;
  width: 16px;
  height: 16px;
  border: 1px solid var(--au-border);
  border-radius: calc(var(--au-r-sm) / 2);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  line-height: 1;
  color: transparent;
  user-select: none;
}
.lpd-box.on {
  background: var(--au-primary);
  border-color: var(--au-primary);
  /* 勾选态：琥珀底上的字色走 --au-on-primary，品牌色覆盖后也保持可读 */
  color: var(--au-on-primary);
}
.lpd-empty, .lpd-error { padding: 24px; text-align: center; font-size: var(--font-size-sm); }
.lpd-empty { color: var(--au-text-4); }
.lpd-error { color: var(--au-danger); }
.lpd-checked {
  margin-top: 8px;
  font-size: var(--font-size-sm);
  color: var(--au-text-2);
}
.lpd-checked b { color: var(--au-primary); font-size: 15px; font-variant-numeric: tabular-nums; }
.lpd-clear { margin-left: 10px; cursor: pointer; color: var(--au-primary); font-size: var(--font-size-xs); }
@media (max-width: 768px) {
  .lpd-toolbar { flex-wrap: wrap; }
  .lpd-toolbar .el-input { flex: 1 1 100%; }
  .lpd-row { padding: 10px 12px; }
}
</style>