<script setup lang="ts">
/**
 * 挂载来源编辑器：四种类型切换 + 前缀自动带
 *
 * 类型**只是路径前缀的函数**（与后端 mounts.detect_mount_type 同一张表）：
 * 本地 = 任意绝对路径、115 = `115:/` 开头、rclone = `rclone:` 开头、
 * Google 云盘 = `drive:` 开头（后接共享盘 ID，API 直读不经 rclone）。
 * 所以这里不给「类型」下拉以外的自由输入——切了类型，前缀自动补上，
 * 不会出现「选了 115、路径写成本机目录」这种存下去才发现的错。
 */
import { computed } from 'vue'

const TYPES = [
  { value: 'local', label: '本地硬盘', prefix: '', placeholder: '/media/movies（任意绝对路径）' },
  { value: '115', label: '115 网盘', prefix: '115:/', placeholder: '115:/0（0 = 根目录）' },
  { value: 'rclone', label: 'rclone', prefix: 'rclone:', placeholder: 'rclone:gdrive/Movies' },
  { value: 'drive', label: 'Google 云盘', prefix: 'drive:', placeholder: 'drive:0AER…（共享盘 ID）' },
] as const

export type MountTypeValue = (typeof TYPES)[number]['value']

const props = withDefaults(defineProps<{
  modelValue: string
  type?: MountTypeValue
  disabled?: boolean
}>(), {
  type: 'local',
  disabled: false,
})

const emit = defineEmits<{
  'update:modelValue': [string]
  'update:type': [MountTypeValue]
}>()

/** 当前类型对应的前缀；本地是空串（绝对路径本身就是前缀） */
function prefixOf(t: string): string {
  return TYPES.find((x) => x.value === t)?.prefix ?? ''
}

/** 换类型时把旧前缀换成新前缀，而不是整段清空（少打一遍字） */
function switchType(next: MountTypeValue) {
  emit('update:type', next)
  const old = prefixOf(props.type)
  const raw = (props.modelValue || '').trim()
  const body = old && raw.startsWith(old) ? raw.slice(old.length) : raw.replace(/^\/+/, '')
  emit('update:modelValue', `${prefixOf(next)}${body}`)
}

const placeholder = computed(() => TYPES.find((x) => x.value === props.type)?.placeholder || '')
</script>

<template>
  <div class="mount-source-editor">
    <el-radio-group
      :model-value="type"
      size="small"
      :disabled="disabled"
      @update:model-value="(v: string | number | boolean | undefined) => switchType(v as MountTypeValue)"
    >
      <el-radio-button v-for="t in TYPES" :key="t.value" :value="t.value">
        {{ t.label }}
      </el-radio-button>
    </el-radio-group>
    <el-input
      :model-value="modelValue"
      :placeholder="placeholder"
      :disabled="disabled"
      @update:model-value="(v: string) => emit('update:modelValue', v)"
    />
    <p class="ms-hint">
      前缀由类型决定：本地任意绝对路径 · 115 以 <code>115:/</code> 开头 · rclone 以
      <code>rclone:</code> 开头
    </p>
  </div>
</template>

<style scoped>
.mount-source-editor { display: flex; flex-direction: column; gap: 8px; width: 100%; }
.ms-hint {
  color: var(--au-text-3);
  font-size: var(--font-size-xs);
  margin: 0;
  line-height: 1.6;
}
.ms-hint code {
  font-family: var(--font-mono);
  padding: 0 4px;
  border-radius: calc(var(--au-r-sm) / 2);
  background: var(--au-bg-soft);
  border: 1px solid var(--au-border);
  color: var(--au-text-2);
}
@media (max-width: 768px) {
  .mount-source-editor :deep(.el-radio-group) { flex-wrap: wrap; }
}
</style>
