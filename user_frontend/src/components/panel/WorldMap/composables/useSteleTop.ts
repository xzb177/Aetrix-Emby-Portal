import { computed, ref, watch } from 'vue'
import { panelApi } from '@/composables/usePanelApi'

/**
 * 观影碑：排行榜数据 + 副标题独立数据源。
 * 从 WorldMap.vue 拆出（原内联逻辑，行为不变）。
 *
 * 副标题口径：固定「今日热播第一名」（loadSteleTop 拉 hot/day 第一名），
 * 不受排行榜弹层浏览状态影响。
 */
export function useSteleTop(params: {
  openSheet: (kind: 'stele') => void
  /** 今日观影人数（副标题回落口径用） */
  todayViewers: () => number
  /** 昵称（排行榜「我」判定用，与 index 保持同一口径） */
  nickname: () => string
}) {
  const nickname = computed(() => params.nickname())

  const lbBoard = ref<'hot' | 'duration'>('hot')
  const lbPeriod = ref<'day' | 'week'>('day')
  const lbData = ref<{ items: Array<Record<string, unknown>>; my_rank: number | null } | null>(null)
  const lbLoading = ref(false)

  async function loadLeaderboard() {
    lbLoading.value = true
    try {
      const d = await panelApi.leaderboard(lbBoard.value, lbPeriod.value)
      lbData.value = { items: d.items ?? [], my_rank: d.my_rank ?? null }
    } catch {
      lbData.value = null
    } finally {
      lbLoading.value = false
    }
  }

  /* 观影碑副标题独立数据源 */
  const steleTop = ref<Record<string, unknown> | null>(null)
  const steleLoaded = ref(false)

  async function loadSteleTop() {
    try {
      const res = await panelApi.leaderboard('hot', 'day')
      const items = ((res as { items?: Record<string, unknown>[] } | null)?.items ?? []) as Record<string, unknown>[]
      steleTop.value = items.length ? items[0] : null
      steleLoaded.value = true
    } catch {
      /* 静默失败：不影响地图其他加载；loaded 保持 false，副标题回落中性标签 */
      steleTop.value = null
    }
  }

  function openStele() {
    void loadLeaderboard()
    params.openSheet('stele')
  }

  watch([lbBoard, lbPeriod], () => {
    void loadLeaderboard()
  })

  const lbRows = computed(() => lbData.value?.items ?? [])
  const lbMyRank = computed(() => lbData.value?.my_rank ?? null)
  const lbScopeText = computed(() =>
    lbPeriod.value === 'day' ? '今日（自然日）· 全服' : '本周（自然周）· 全服',
  )

  function lbTitle(r: Record<string, unknown>): string {
    return lbBoard.value === 'hot' ? String(r.name ?? '') : String(r.nickname ?? '')
  }
  function lbNumText(r: Record<string, unknown>): string {
    return lbBoard.value === 'hot' ? String(Number(r.count ?? 0)) : fmtHours(r.seconds).replace(/\s*小时$/, '')
  }
  function lbUnitText(): string {
    return lbBoard.value === 'hot' ? '次' : '小时'
  }
  function lbIsMe(r: Record<string, unknown>): boolean {
    return lbBoard.value === 'duration' && String(r.nickname ?? '') === nickname.value
  }
  const lbPodium = computed(() => {
    const top = lbRows.value.slice(0, 3).map((r, i) => ({
      rank: i + 1,
      title: lbTitle(r),
      type: String(r.item_type ?? ''),
      num: lbNumText(r),
      unit: lbBoard.value === 'hot' ? (i === 0 && lbBoard.value === 'hot' ? '次播放' : '次') : '小时',
    }))
    /* 视觉顺序：亚军左、冠军中、季军右 */
    const order = [top[1], top[0], top[2]]
    return order.filter((s) => s !== undefined)
  })
  const lbRest = computed(() => lbRows.value.slice(3))
  const lbMyRow = computed(() =>
    lbBoard.value === 'duration' ? lbRows.value.find((r) => lbIsMe(r)) ?? null : null,
  )
  const lbMyGap = computed<string | null>(() => {
    const rank = lbMyRank.value
    if (lbBoard.value !== 'duration' || !rank || rank <= 1 || !lbMyRow.value) return null
    const prev = lbRows.value[rank - 2]
    if (!prev) return null
    const gapSec = Number(prev.seconds ?? 0) - Number(lbMyRow.value.seconds ?? 0)
    if (!Number.isFinite(gapSec) || gapSec <= 0) return null
    const g = fmtHours(gapSec)
    return g === '0 小时' ? null : g
  })
  const lbStampTop = computed(() =>
    lbBoard.value === 'duration' && lbMyRank.value ? String(lbMyRank.value) : '榜',
  )
  const lbStampBottom = computed(() =>
    lbBoard.value === 'duration' && lbMyRank.value ? '名' : '外',
  )
  const lbMyLead = computed(() => {
    if (lbBoard.value === 'hot') return '热播榜是全服的榜，你的口味在'
    if (!lbMyRank.value) return '你还没上榜——去看一部片子，把名字刻上碑。'
    return `你${lbPeriod.value === 'day' ? '今天' : '本周'}排在`
  })
  const lbMyStrong = computed(() => {
    if (lbBoard.value === 'hot') return '时长榜'
    if (!lbMyRank.value) return ''
    return `第 ${lbMyRank.value} 名`
  })
  const lbMyTail = computed(() => {
    if (lbBoard.value === 'hot') return '见分晓——去看看你排第几。'
    if (!lbMyRank.value) return ''
    if (lbMyRank.value === 1) return '，碑顶就是你，守住它。'
    return lbMyGap.value ? '——再看一部就追上了。' : '。'
  })
  const lbScopeLine = computed(() =>
    lbBoard.value === 'hot'
      ? `${lbScopeText.value} · 按全服播放次数统计`
      : `${lbScopeText.value} · 时长榜只显示昵称，可在「我的账号 → 设置」里关掉自己上榜`,
  )

  const steleSub = computed(() => {
    if (!steleLoaded.value) return '观影碑'
    const viewers = params.todayViewers()
    const first = steleTop.value
    if (!first && viewers > 0) return `今日已有 ${viewers} 人观影`
    if (!first) return '虚位以待，等你开播'
    const name = String(first.name ?? first.nickname ?? '')
    const count = Number(first.count ?? 0)
    return `今日最热 · ${name} ${count}次`
  })

  function fmtHours(v: unknown): string {
    const s = Number(v ?? 0)
    if (!Number.isFinite(s) || s <= 0) return '0 小时'
    const h = Math.round((s / 3600) * 10) / 10
    return `${h} 小时`
  }

  return {
    lbBoard,
    lbPeriod,
    lbData,
    lbLoading,
    loadLeaderboard,
    steleTop,
    steleLoaded,
    loadSteleTop,
    openStele,
    lbRows,
    lbMyRank,
    lbScopeText,
    lbTitle,
    lbNumText,
    lbUnitText,
    lbIsMe,
    lbPodium,
    lbRest,
    lbMyRow,
    lbMyGap,
    lbStampTop,
    lbStampBottom,
    lbMyLead,
    lbMyStrong,
    lbMyTail,
    lbScopeLine,
    steleSub,
    fmtHours,
  }
}
