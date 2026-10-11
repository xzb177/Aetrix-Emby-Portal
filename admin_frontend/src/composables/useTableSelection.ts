import { ref, computed } from 'vue'

/** 行键类型：与 DataTable 的 rowKey 取值保持一致 */
export type RowKey = string | number

/** 表格行选择的极简共享状态。toggle 逻辑收进 DataTable 组件内部，view 层只需要 selectedKeys + clear。 */
export function useTableSelection() {
  /** 当前选中的行键：由 DataTable 通过 v-model:selectedKeys 回写 */
  const selectedKeys = ref<RowKey[]>([])

  /** 当前选中数量（驱动批量工具条的显隐与「已选 N 项」） */
  const selectedCount = computed(() => selectedKeys.value.length)

  /** 清空所有选择：筛选提交、操作完成后调用 */
  function clear() {
    selectedKeys.value = []
  }

  return { selectedKeys, selectedCount, clear }
}
