<script setup lang="ts">
/**
 * 服管理：一个面板同时运营多个服
 *
 * 「管什么」这个项目里最容易被混淆的一件事，先在这里说清：
 *
 * - **服（realm）**＝一套可以独立运营的播放服务：自己的媒体库、存储挂载、套餐、订阅、会员卡码、求片；
 * - **一个服可以有多台播放节点（EA）**：同一套服务部署到多台机器上，各放自己碰得到的内容；
 * - **面板顶部切的是「当前服」**：切过去之后，订阅、套餐、媒体库、挂载、服务器都只看这个服。
 *
 * 所以这一页只做四件事：看清单（带每服的运营数据）、新建 / 改名、切换当前服、删服（数据可移交）。
 *
 * v2.54（暗房影院）：PageHeader + StatTile 总览；每个服是一张 SectionCard（当前服走琥珀发丝描边，
 * 不再有位移 / 光晕），统计格、节点列表、操作区全部改走 --au-* 令牌；
 * 首次加载有骨架、加载失败 / 没有服时给 EmptyState。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  AlertTriangle, CheckCircle2, Crown, Film, Info, Network, Pencil, Plus,
  RefreshCw, Route, Server, Trash2, Wifi,
} from 'lucide-vue-next'
import { EmptyState, PageHeader, SectionCard, StatTile } from '@/components/ui'
import { createRealm, deleteRealm, syncRealmNodes, updateRealm } from '@/api/admin'
import { useRealmStore } from '@/stores/realm'
import type { RealmNodeSync, RealmRow } from '@/types'
import NoticePanel from '@/components/NoticePanel.vue'

const realm = useRealmStore()

const realms = computed(() => realm.realms)
const summary = computed(() => realm.summary)
const activeId = computed(() => realm.activeId)
const loading = ref(false)
const busyId = ref<number | null>(null)
/** 服清单加载失败（区别于「还没有服」） */
const loadError = ref(false)

async function load() {
  loading.value = true
  loadError.value = false
  try {
    await realm.refresh()
  } catch {
    /* 错误提示由 HTTP 拦截器统一处理 */
    loadError.value = true
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  if (!realm.loaded) {
    loading.value = true
    try {
      await realm.load()
    } catch {
      loadError.value = true
    } finally {
      loading.value = false
    }
  }
})

// ==================== 新建 / 编辑 ====================

const dialogVisible = ref(false)
const editing = ref<RealmRow | null>(null)
const saving = ref(false)
// access_mode：paid（付费服，需要订阅）/ free（公益服，免费开放）
// download_policy：follow（跟随全局，公益服默认禁止下载）/ allow / deny
const form = reactive({
  name: '', slug: '', url: '', description: '', is_active: true,
  access_mode: 'paid' as 'paid' | 'free',
  access_note: '',
  download_policy: 'follow' as 'follow' | 'allow' | 'deny',
})

function policyOf(row: RealmRow): 'follow' | 'allow' | 'deny' {
  if (row.allow_download === null || row.allow_download === undefined) return 'follow'
  return row.allow_download ? 'allow' : 'deny'
}

function openCreate() {
  editing.value = null
  Object.assign(form, {
    name: '', slug: '', url: '', description: '', is_active: true,
    access_mode: 'paid', access_note: '', download_policy: 'follow',
  })
  dialogVisible.value = true
}

function openEdit(row: RealmRow) {
  editing.value = row
  Object.assign(form, {
    name: row.name, slug: row.slug, url: row.url,
    description: row.description, is_active: row.is_active,
    access_mode: row.access_mode === 'free' ? 'free' : 'paid',
    access_note: row.access_note || '',
    download_policy: policyOf(row),
  })
  dialogVisible.value = true
}

async function save() {
  if (!form.name.trim()) return ElMessage.warning('请填写服的名称')
  saving.value = true
  try {
    if (editing.value) {
      await updateRealm(editing.value.id, {
        name: form.name.trim(),
        url: form.url.trim(),
        description: form.description.trim(),
        is_active: form.is_active,
        access_mode: form.access_mode,
        access_note: form.access_note.trim(),
        download_policy: form.download_policy,
      })
      ElMessage.success('已保存')
    } else {
      await createRealm({
        name: form.name.trim(),
        slug: form.slug.trim(),
        url: form.url.trim(),
        description: form.description.trim(),
        is_active: form.is_active,
        access_mode: form.access_mode,
        access_note: form.access_note.trim(),
        allow_download: form.download_policy === 'follow' ? null : form.download_policy === 'allow',
      })
      ElMessage.success('已新建，接下来去「媒体库 / 存储来源 / 订阅套餐」里往里填内容')
    }
    dialogVisible.value = false
    await realm.refresh()
  } catch {
    /* 拦截器已提示 */
  } finally {
    saving.value = false
  }
}

// ==================== 切换当前服 ====================

async function switchTo(row: RealmRow) {
  if (row.id === activeId.value) return
  busyId.value = row.id
  try {
    await realm.switchTo(row.id)
    ElMessage.success(`已切换到「${row.name}」，后台各页都只看这个服的数据`)
    // 各页的作用域跟着「当前服」走：整页重载一次，避免停在旧服的数据上
    window.setTimeout(() => window.location.reload(), 400)
  } catch {
    /* 拦截器已提示 */
  } finally {
    busyId.value = null
  }
}

// ==================== 节点体检 ====================

const syncVisible = ref(false)
const syncTarget = ref<RealmRow | null>(null)
const syncResults = ref<RealmNodeSync[]>([])

async function runSync(row: RealmRow) {
  busyId.value = row.id
  try {
    const res = await syncRealmNodes(row.id)
    syncTarget.value = res.realm
    syncResults.value = res.nodes
    syncVisible.value = true
    const online = res.nodes.filter((n) => n.ok).length
    if (res.nodes.length === 0) {
      ElMessage.warning('这个服还没有播放节点：去「服务器」页加一台 EA 并把它的归属服选成这个服')
    } else if (online === res.nodes.length) {
      ElMessage.success(`${res.nodes.length} 台播放节点全部在线`)
    } else {
      ElMessage.warning(`${res.nodes.length - online} / ${res.nodes.length} 台播放节点没通过体检`)
    }
    await realm.refresh()
  } catch {
    /* 拦截器已提示 */
  } finally {
    busyId.value = null
  }
}

// ==================== 删服（数据可移交）====================

const deleteVisible = ref(false)
const deleteTarget = ref<RealmRow | null>(null)
const moveTo = ref<number | null>(null)
const deleting = ref(false)

const deleteStats = computed(() => {
  const s = deleteTarget.value?.stats
  if (!s) return [] as { label: string; value: number }[]
  return [
    { label: '媒体库', value: s.libraries },
    { label: '存储来源', value: s.mounts },
    { label: '套餐', value: s.plans },
    { label: '有效订阅', value: s.active_subscriptions },
  ]
})

const hasData = computed(() => deleteStats.value.some((s) => s.value > 0))
const otherRealms = computed(() => realms.value.filter((r) => r.id !== deleteTarget.value?.id))

function openDelete(row: RealmRow) {
  deleteTarget.value = row
  moveTo.value = null
  deleteVisible.value = true
}

async function confirmDelete() {
  const row = deleteTarget.value
  if (!row) return
  if (hasData.value && !moveTo.value) {
    return ElMessage.warning('这个服还有数据，请先选一个「数据移交给」的服')
  }
  if (!hasData.value) {
    try {
      await ElMessageBox.confirm(`「${row.name}」将被永久删除，无法恢复。`, '删除服', {
        type: 'warning',
        confirmButtonText: '删除这个服',
        cancelButtonText: '取消',
        confirmButtonClass: 'el-button--danger',
      })
    } catch {
      return
    }
  }
  deleting.value = true
  try {
    const res = await deleteRealm(row.id, moveTo.value)
    ElMessage.success(res.moved_to
      ? `已删除，数据已移交到「${realms.value.find((r) => r.id === res.moved_to)?.name || res.moved_to}」`
      : '已删除')
    deleteVisible.value = false
    await realm.refresh()
    window.setTimeout(() => window.location.reload(), 400)
  } catch {
    /* 拦截器已提示（例如默认服不能删） */
  } finally {
    deleting.value = false
  }
}

// ==================== 展示辅助 ====================

function nodesText(row: RealmRow): string {
  if (row.nodes.length === 0) return '还没有播放节点'
  return `${row.stats.nodes_online} / ${row.nodes.length} 台在线`
}

function nodeBadge(node: RealmRow['nodes'][number]): string {
  if (!node.is_enabled) return 'au-badge-muted'
  if (node.online) return 'au-badge-green'
  return 'badge-warn'
}

function nodeText(node: RealmRow['nodes'][number]): string {
  if (!node.is_enabled) return '已停用'
  return node.online ? '在线' : '未通过'
}

function shortDate(s: string | null): string {
  return s ? s.slice(0, 16).replace('T', ' ') : '未体检'
}
</script>

<template>
  <div class="admin-page realms-page">
    <PageHeader
      eyebrow="媒体"
      title="服管理"
      description="一个面板可以同时运营多个服。一个服＝一套独立的播放服务：自己的媒体库、存储来源、套餐、订阅、会员卡码与求片；同一个服可以部署到多台机器，每台机器就是一台播放节点。"
    >
      <template #actions>
        <el-button :icon="RefreshCw" :loading="loading" @click="load">刷新</el-button>
        <el-button type="primary" :icon="Plus" @click="openCreate">新建服</el-button>
      </template>
    </PageHeader>

    <section class="stat-row" aria-label="多服总览">
      <StatTile label="服" :value="summary?.total_realms ?? 0" :icon="Route" :hint="`${summary?.enabled_realms ?? 0} 个启用中`" />
      <StatTile
        label="有效订阅"
        :value="summary?.active_subscriptions ?? 0"
        :icon="Crown"
        :hint="`${summary?.subscribers ?? 0} 位订阅用户（全部服合计）`"
      />
      <StatTile label="媒体库" :value="summary?.libraries ?? 0" :icon="Film" :hint="`${summary?.items ?? 0} 个条目`" />
      <StatTile
        label="播放节点在线"
        :value="summary?.nodes_online ?? 0"
        :suffix="`/ ${summary?.nodes ?? 0}`"
        :icon="Network"
        :tone="(summary?.nodes ?? 0) > (summary?.nodes_online ?? 0) ? 'warn' : 'plain'"
        hint="在线 / 总数，一个服可以有多台"
      />
    </section>

    <NoticePanel
      title="「一个服一个」的东西有哪些"
      summary="套餐、订阅、媒体库、存储与会员卡码都按当前服隔离"
      :icon="Info"
      storage-key="realms-scope"
    >
      <div class="guide">
        套餐、订阅、媒体库、存储来源、服务器与线路、会员卡码、求片 —— 这些都属于某一个服，
        面板顶部切换「当前服」之后，各页默认只显示那个服的数据。
        <b>会员按服计算</b>：用户可以在 A 服和 B 服各有一份会员，互不影响，哪台节点能播也按服判定。
        默认服沿用历史配置，不能删除，但可以改名。
      </div>
    </NoticePanel>

    <!-- 首屏骨架：与卡片同形 -->
    <div v-if="loading && !realms.length" class="realm-grid" aria-busy="true" aria-label="加载中">
      <div v-for="n in 2" :key="n" class="au-skeleton sk-card" />
    </div>

    <SectionCard v-else-if="!realms.length">
      <EmptyState
        :icon="loadError ? AlertTriangle : Route"
        :title="loadError ? '服清单加载失败' : '还没有服'"
        :description="loadError ? '网络或服务暂时不可用，稍后重试。' : '新建一个服，再去媒体库 / 存储来源 / 订阅套餐里往里填内容。'"
      >
        <template #actions>
          <el-button v-if="loadError" :loading="loading" @click="load">重试</el-button>
          <el-button v-else type="primary" :icon="Plus" @click="openCreate">新建服</el-button>
        </template>
      </EmptyState>
    </SectionCard>

    <div v-else class="realm-grid">
      <SectionCard
        v-for="row in realms"
        :key="row.id"
        as="article"
        class="realm-card"
        :class="{ 'is-off': !row.is_active }"
        :tone="row.id === activeId ? 'accent' : 'default'"
      >
        <template #title>
          <span class="realm-icon"><Route :size="16" /></span>
          <span class="realm-title">
            <span class="realm-name">{{ row.name }}</span>
            <span class="realm-slug mono">{{ row.slug }}</span>
          </span>
        </template>
        <template #actions>
          <div class="realm-badges">
            <span v-if="row.id === activeId" class="au-badge au-badge-amber">当前服</span>
            <span v-if="row.is_default" class="au-badge au-badge-info">默认服</span>
            <!-- 接入方式：公益服免费开放（不需要订阅），付费服按订阅闸门 -->
            <span v-if="row.is_free" class="au-badge au-badge-green">公益服</span>
            <span v-else class="au-badge au-badge-muted">付费服</span>
            <span v-if="!row.is_active" class="au-badge au-badge-rose">已停用</span>
          </div>
        </template>

        <div class="realm-body">
          <p v-if="row.description" class="realm-desc">{{ row.description }}</p>
          <p v-if="row.is_free" class="realm-free">
            免费开放（无需订阅）· 下载{{ policyOf(row) === 'allow' ? '允许' : '禁止' }}
          </p>
          <p class="realm-url">
            <template v-if="row.public_url">用户端地址：<code>{{ row.public_url }}</code></template>
            <span v-else class="realm-url-missing">还没填对外地址：用户端拿不到这个服的连接地址</span>
          </p>

          <dl class="realm-stats">
            <div><dt>媒体库</dt><dd>{{ row.stats.libraries }}</dd></div>
            <div><dt>条目</dt><dd>{{ row.stats.items }}</dd></div>
            <div><dt>挂载</dt><dd>{{ row.stats.mounts }}</dd></div>
            <div><dt>套餐</dt><dd>{{ row.stats.plans }}</dd></div>
            <div><dt>有效订阅</dt><dd>{{ row.stats.active_subscriptions }}</dd></div>
            <div><dt>待审求片</dt><dd>{{ row.stats.pending_requests }}</dd></div>
          </dl>

          <div class="realm-nodes">
            <div class="nodes-head">
              <Network :size="13" /> 播放节点 · {{ nodesText(row) }}
            </div>
            <ul v-if="row.nodes.length" class="node-list">
              <li v-for="node in row.nodes" :key="node.id">
                <Server :size="12" class="node-icon" />
                <span class="node-name">{{ node.name }}</span>
                <span class="au-badge" :class="nodeBadge(node)">{{ nodeText(node) }}</span>
                <span class="node-time">{{ shortDate(node.last_checked_at) }}</span>
              </li>
            </ul>
            <p v-else class="nodes-empty">
              还没有认领的节点：在「服务器」页加一台后端服（EA）并把「归属服」选成本服，
              再给那台机器配 <code>NODE_KEY</code> 启动即可
            </p>
          </div>
        </div>

        <template #footer>
          <div class="realm-actions">
            <el-button
              v-if="row.id !== activeId"
              type="primary"
              size="small"
              :icon="CheckCircle2"
              :disabled="!row.is_active"
              :loading="busyId === row.id"
              @click="switchTo(row)"
            >
              切换到此服
            </el-button>
            <span v-else class="current-hint">正在运营这个服</span>
            <span class="realm-actions__right">
              <el-button size="small" :icon="Wifi" :loading="busyId === row.id" @click="runSync(row)">节点体检</el-button>
              <el-button size="small" :icon="Pencil" aria-label="编辑" title="编辑" @click="openEdit(row)" />
              <el-button
                size="small"
                type="danger"
                plain
                :icon="Trash2"
                aria-label="删除"
                :title="row.is_default ? '默认服不能删除' : '删除'"
                :disabled="row.is_default"
                @click="openDelete(row)"
              />
            </span>
          </div>
        </template>
      </SectionCard>
    </div>

    <!-- 新建 / 编辑 -->
    <el-dialog v-model="dialogVisible" :title="editing ? '编辑服' : '新建服'" width="min(520px, 92vw)">
      <el-form label-position="top">
        <el-form-item label="名称">
          <el-input v-model="form.name" placeholder="给运营看的名字，例如「主站 · 月付」" />
        </el-form-item>
        <el-form-item label="标识（slug）">
          <el-input
            v-model="form.slug"
            :disabled="!!editing"
            placeholder="留空自动生成；EA 用 REALM 环境变量认领时填这个"
          />
          <p class="field-help">只能用小写字母、数字、下划线或短横线，≤40 字符；建好后不可改。</p>
        </el-form-item>
        <el-form-item label="对外地址（用户端拿到的 Emby 地址）">
          <el-input v-model="form.url" placeholder="https://media.example.com，留空则用「服务器」页里那台 EA 的地址" />
          <p class="field-help">多服部署时每个服各自的地址，用户端账号卡会用它。</p>
        </el-form-item>
        <el-form-item label="接入方式">
          <el-radio-group v-model="form.access_mode">
            <el-radio-button value="paid">付费服（需要订阅）</el-radio-button>
            <el-radio-button value="free">公益服（免费开放）</el-radio-button>
          </el-radio-group>
          <p class="field-help">
            付费服：用户要有生效中的订阅才能播放（原来的口径）。
            公益服：不需要订阅就能看全库，用于免费引流；默认禁止下载，可用下面的下载策略覆盖。
          </p>
        </el-form-item>
        <el-form-item v-if="form.access_mode === 'free'" label="公益规则（用户端展示）">
          <el-input
            v-model="form.access_note"
            type="textarea"
            :rows="3"
            placeholder="留空则用默认文案：本服为公益服 · 免费开放：无需开通会员即可观看全库内容。资源请勿下载、转卖或外传，账号仅限本人使用。"
          />
          <p class="field-help">写清免费开放的边界（限设备 / 禁下载 / 禁止转卖），会展示在用户端首页与个人中心。</p>
        </el-form-item>
        <el-form-item label="下载策略">
          <el-select v-model="form.download_policy" style="width: 100%">
            <el-option value="follow" label="跟随全局（公益服默认禁止下载）" />
            <el-option value="allow" label="允许下载" />
            <el-option value="deny" label="禁止下载" />
          </el-select>
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="form.description" type="textarea" :rows="2" placeholder="可选，例如面向哪些用户" />
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="form.is_active" />
          <p class="field-help">停用后不能切换成「当前服」，但数据保留，可以随时再启用。</p>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>

    <!-- 节点体检结果 -->
    <el-dialog v-model="syncVisible" :title="`「${syncTarget?.name || ''}」的节点体检结果`" width="min(680px, 92vw)">
      <p class="sync-lead">
        体检会逐台连接该服的播放节点，核对 <code>NODE_KEY</code>（哪台机器）、
        <code>REALM</code>（属于哪个服）与它负责的媒体库数量。
      </p>
      <ul class="sync-list">
        <li v-for="n in syncResults" :key="n.id" :class="n.ok ? 'ok' : 'bad'">
          <span class="sync-name">{{ n.name }}</span>
          <span class="sync-url">{{ n.url }}</span>
          <span class="au-badge" :class="n.ok ? 'au-badge-green' : 'au-badge-rose'">{{ n.ok ? '可达' : '不可达' }}</span>
          <span class="au-badge au-badge-muted">{{ n.libraries }} 个库</span>
          <span v-if="n.node_key_claimed" class="au-badge au-badge-muted">key: {{ n.node_key_claimed }}</span>
          <div v-if="n.realm_mismatch" class="sync-warn">
            <AlertTriangle :size="13" />
            该节点自称属于「{{ n.realm_slug_reported }}」，与这个服（{{ syncTarget?.slug }}）不一致：
            检查那台机器的 <code>REALM</code> 配置
          </div>
          <div v-else-if="!n.ok && n.message" class="sync-msg">{{ n.message }}</div>
        </li>
      </ul>
      <EmptyState v-if="!syncResults.length" compact :icon="Network" title="这个服还没有播放节点" />
      <template #footer>
        <el-button type="primary" @click="syncVisible = false">知道了</el-button>
      </template>
    </el-dialog>

    <!-- 删服 -->
    <el-dialog v-model="deleteVisible" title="删除服" width="min(520px, 92vw)">
      <p class="delete-lead">
        确认删除「{{ deleteTarget?.name }}」？默认服不能删除（历史配置挂在它上面）。
      </p>
      <template v-if="hasData">
        <div class="delete-stats">
          <span v-for="s in deleteStats" :key="s.label" class="au-badge au-badge-muted">
            {{ s.label }} {{ s.value }}
          </span>
        </div>
        <p class="delete-lead">这个服还有数据，删除前必须指定一个服把数据整体移交过去：</p>
        <el-select v-model="moveTo" placeholder="选择数据移交到的服" style="width: 100%">
          <el-option v-for="r in otherRealms" :key="r.id" :label="r.name" :value="r.id" />
        </el-select>
        <p class="field-help">
          媒体库、存储来源、套餐、订阅、会员卡码、求片与播放节点会一起移过去；对应用户的会员继续有效。
        </p>
      </template>
      <template #footer>
        <el-button @click="deleteVisible = false">取消</el-button>
        <el-button
          type="danger"
          :loading="deleting"
          :disabled="hasData && !moveTo"
          @click="confirmDelete"
        >
          删除这个服
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

.guide { line-height: 1.75; }

.realm-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(380px, 1fr));
  gap: 16px;
  align-items: stretch;
}

.sk-card { height: 300px; border-radius: var(--au-r-lg); }

/* 每个服一张卡：分层靠发丝线，当前服由 SectionCard tone=accent 给琥珀描边 */
.realm-card { display: flex; flex-direction: column; }
.realm-card :deep(.au-section__body) { flex: 1; }
.realm-card.is-off { opacity: 0.72; }

.realm-icon {
  display: inline-grid;
  place-items: center;
  width: 30px;
  height: 30px;
  flex-shrink: 0;
  border-radius: var(--au-r-md);
  background: var(--au-primary-soft);
  border: 1px solid var(--au-primary-border);
  color: var(--au-primary);
}
.realm-title { display: inline-flex; flex-direction: column; min-width: 0; line-height: 1.3; }
.realm-name { font-size: 16px; color: var(--au-text); overflow-wrap: anywhere; }
.realm-slug { font-size: 11.5px; font-weight: 400; letter-spacing: 0; color: var(--au-text-4); }
.realm-badges { display: flex; gap: 6px; flex-wrap: wrap; }

.realm-body { display: flex; flex-direction: column; gap: 12px; }
.realm-desc { margin: 0; font-size: 13px; line-height: 1.6; color: var(--au-text-2); }
/* 公益服：免费开放与下载口径，一眼能看出这个服不靠会员收费 */
.realm-free { margin: 0; font-size: 12px; color: var(--au-success); }
.realm-url { margin: 0; font-size: 12px; color: var(--au-text-3); word-break: break-all; }
.realm-url code { color: var(--au-text-2); }
.realm-url-missing { color: var(--au-warning); }

.realm-stats {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 6px 12px;
  margin: 0;
  padding: 10px 12px;
  background: var(--au-surface-2);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-md);
}
.realm-stats div { display: flex; align-items: baseline; justify-content: space-between; gap: 6px; }
.realm-stats dt { font-size: 12px; color: var(--au-text-3); }
.realm-stats dd { margin: 0; font-size: 13px; font-weight: 600; font-variant-numeric: tabular-nums; color: var(--au-text); }

.realm-nodes { display: flex; flex-direction: column; gap: 6px; }
.nodes-head { display: flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 500; color: var(--au-text-3); }
.node-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.node-list li { display: flex; align-items: center; gap: 8px; min-width: 0; font-size: 12px; color: var(--au-text-2); }
.node-icon { color: var(--au-text-4); flex-shrink: 0; }
.node-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 500; color: var(--au-text); }
.node-time { margin-left: auto; white-space: nowrap; font-variant-numeric: tabular-nums; color: var(--au-text-4); }
.nodes-empty { margin: 0; font-size: 12px; line-height: 1.65; color: var(--au-text-3); }

.realm-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.realm-actions__right { display: inline-flex; align-items: center; gap: 6px; margin-left: auto; }
.realm-actions .el-button + .el-button { margin-left: 0; }
.current-hint { font-size: 12px; font-weight: 600; color: var(--au-primary); }

.badge-warn { background: var(--au-warning-soft); color: var(--au-warning); border-color: var(--au-warning-border); }

/* ---------- 弹窗 ---------- */
.field-help { margin: 5px 0 0; font-size: 12px; line-height: 1.6; color: var(--au-text-3); }

.sync-lead, .delete-lead { margin: 0 0 10px; font-size: 13px; line-height: 1.7; color: var(--au-text-2); }
.sync-lead code, .delete-lead code { color: var(--au-text); }
.sync-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.sync-list li {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 10px 12px;
  background: var(--au-surface-2);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-md);
}
.sync-list li.ok { border-color: var(--au-success-border); }
.sync-list li.bad { border-color: var(--au-danger-border); }
.sync-name { font-size: 13px; font-weight: 500; color: var(--au-text); }
.sync-url { font-size: 12px; word-break: break-all; color: var(--au-text-3); }
.sync-warn, .sync-msg { flex-basis: 100%; display: flex; align-items: center; gap: 6px; font-size: 12px; line-height: 1.6; }
.sync-warn { color: var(--au-danger); }
.sync-msg { color: var(--au-text-3); }
.delete-stats { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 10px; }

@media (max-width: 900px) {
  .realm-grid { grid-template-columns: 1fr; }
}

@media (max-width: 768px) {
  .realm-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .realm-actions > .el-button { flex: 1 1 100%; }
  .current-hint { flex: 1 1 100%; }
  .realm-actions__right { margin-left: 0; width: 100%; }
  .realm-actions__right > .el-button:first-child { flex: 1; }
}
</style>
