<template>
  <section class="subtitle-picker">
    <h3>字幕</h3>
    <p class="note">
      把你手头的字幕文件名贴进来（每行一个），按片名、季、集帮你对上这一集，选一条记下来。
    </p>
    <textarea
      v-model="candidatesText"
      rows="3"
      placeholder="例如：Movie.2024.chi.srt"
      aria-label="字幕文件名，每行一个"
    />
    <button type="button" :disabled="matching || !hasCandidates" @click="onMatch">
      {{ matching ? '正在匹配…' : '匹配' }}
    </button>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-else-if="matched !== null && matched.length === 0" class="note">
      没有对上的字幕，换一批文件名再试。
    </p>
    <ul v-if="matched && matched.length" class="matched">
      <li v-for="m in matched" :key="m.name">
        <label>
          <input type="radio" :value="m.name" v-model="picked" @change="savePick" />
          <span>{{ m.name }}</span>
          <span class="lang">{{ langLabel(m.language) }}</span>
        </label>
      </li>
    </ul>
    <p v-if="picked" class="note">已选：{{ picked }}</p>
    <p class="note">
      双语字幕生成：需要服主在系统设置→能力中心配好翻译提供方，当前还没配，暂时生成不了。
    </p>
    <p class="note">
      点播后在播放器里选字幕轨；这里的匹配和选择是帮你对上字幕文件，实际渲染由播放器决定。
    </p>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { subtitleApi, type MatchedSubtitle } from '@/api/subtitles'

const props = defineProps<{
  itemId: number
}>()

const candidatesText = ref('')
const matching = ref(false)
const error = ref('')
const matched = ref<MatchedSubtitle[] | null>(null)
const picked = ref('')

const hasCandidates = computed(() =>
  candidatesText.value.split('\n').some((line) => line.trim() !== ''),
)

function pickKey(): string {
  return `aetrix-subtitle-pick:${props.itemId}`
}

function langLabel(code: string): string {
  if (code === 'chi') return '中文'
  if (code === 'eng') return '英文'
  return code
}

function errText(e: unknown): string {
  const status = (e as { response?: { status?: number } })?.response?.status
  const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data
    ?.detail
  if (status === 403) {
    if (typeof detail === 'string' && detail.includes('智能字幕未开启')) {
      return '智能字幕未开启：请服主在系统设置→能力中心开启字幕后再试。'
    }
    return '该条目不在你的可见范围内。'
  }
  if (status === 404) return '条目不存在。'
  return typeof detail === 'string' && detail ? detail : '匹配失败，稍后再试。'
}

async function onMatch() {
  const candidates = candidatesText.value
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line !== '')
  if (candidates.length === 0) return
  matching.value = true
  error.value = ''
  try {
    const res = await subtitleApi.match(props.itemId, candidates)
    matched.value = res.matched
    if (!res.matched.some((m) => m.name === picked.value)) {
      picked.value = ''
      localStorage.removeItem(pickKey())
    }
  } catch (e: unknown) {
    error.value = errText(e)
    matched.value = null
  } finally {
    matching.value = false
  }
}

function savePick() {
  if (picked.value) {
    localStorage.setItem(pickKey(), picked.value)
  }
}

onMounted(() => {
  const saved = localStorage.getItem(pickKey())
  if (saved) picked.value = saved
})
</script>

<style scoped>
.subtitle-picker {
  margin-top: 16px;
  padding-top: 16px;
  border-top: 1px solid #ddd;
}

.subtitle-picker textarea {
  display: block;
  width: 100%;
  box-sizing: border-box;
  margin: 8px 0;
}

.note {
  color: #666;
  font-size: 14px;
}

.error {
  color: #b3261e;
}

.matched {
  list-style: none;
  padding: 0;
  margin: 8px 0;
}

.matched li {
  padding: 4px 0;
}

.lang {
  color: #666;
  font-size: 13px;
  margin-left: 6px;
}
</style>
