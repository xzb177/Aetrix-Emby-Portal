<template>
  <div v-if="entry.loading" class="v4-skeleton" style="height:80px"></div>
  <div v-else-if="entry.error" class="v4-err">{{ label }}加载失败：{{ entry.error }}</div>
  <pre v-else class="v4-err" style="white-space:pre-wrap">{{ text }}</pre>
</template>

<script setup lang="ts">
import { computed } from 'vue'

interface CacheEntry {
  data: unknown
  error: string | null
  loading: boolean
}

const props = defineProps<{
  entry: CacheEntry
  label: string
}>()

const text = computed(() => JSON.stringify(props.entry.data, null, 2))
</script>
