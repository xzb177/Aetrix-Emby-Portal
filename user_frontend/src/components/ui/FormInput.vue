<template>
  <div :class="['neo-input', { 'neo-input--error': error, 'neo-input--disabled': disabled, 'neo-input--focused': focused }]">
    <label v-if="label" :class="['neo-input__label', { 'neo-input__label--required': required }]">
      {{ label }}
    </label>

    <div class="neo-input__wrapper">
      <span v-if="prefixIcon" class="neo-input__icon neo-input__icon--prefix">
        <component :is="prefixIcon" />
      </span>

      <input
        :id="id"
        ref="inputRef"
        v-model="modelValue"
        :type="type"
        :placeholder="placeholder"
        :disabled="disabled"
        :readonly="readonly"
        :maxlength="maxlength"
        :autocomplete="autocomplete"
        :class="['neo-input__field', { 'neo-input__field--has-icon': prefixIcon || suffixIcon }]"
        @focus="handleFocus"
        @blur="handleBlur"
      />

      <span v-if="suffixIcon" class="neo-input__icon neo-input__icon--suffix">
        <component :is="suffixIcon" />
      </span>

      <button
        v-if="clearable && modelValue && !disabled"
        type="button"
        aria-label="清空"
        class="neo-input__clear"
        @click="handleClear"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="12" cy="12" r="10"/>
          <path d="M15 9l-6 6M9 9l6 6"/>
        </svg>
      </button>
    </div>

    <div v-if="error || hint" class="neo-input__message">
      <span v-if="error" class="neo-input__error">{{ error }}</span>
      <span v-else-if="hint" class="neo-input__hint">{{ hint }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'

export interface FormInputProps {
  id?: string
  type?: 'text' | 'password' | 'email' | 'number' | 'tel' | 'url'
  modelValue: string | number
  label?: string
  placeholder?: string
  error?: string
  hint?: string
  disabled?: boolean
  readonly?: boolean
  required?: boolean
  clearable?: boolean
  maxlength?: number
  autocomplete?: string
  prefixIcon?: any
  suffixIcon?: any
}

const props = withDefaults(defineProps<FormInputProps>(), {
  type: 'text',
  disabled: false,
  readonly: false,
  required: false,
  clearable: false
})

const emit = defineEmits<{
  'update:modelValue': [value: string | number]
  focus: [event: FocusEvent]
  blur: [event: FocusEvent]
  clear: []
}>()

const focused = ref(false)
const inputRef = ref<HTMLInputElement>()

const modelValue = computed({
  get: () => props.modelValue,
  set: (value) => emit('update:modelValue', value)
})

const handleFocus = (e: FocusEvent) => {
  focused.value = true
  emit('focus', e)
}

const handleBlur = (e: FocusEvent) => {
  focused.value = false
  emit('blur', e)
}

const handleClear = () => {
  modelValue.value = ''
  emit('clear')
}

defineExpose({
  focus: () => inputRef.value?.focus(),
  blur: () => inputRef.value?.blur()
})
</script>

<style scoped>
.neo-input {
  display: flex;
  flex-direction: column;
  gap: var(--neo-space-2);
}

.neo-input__label {
  font-size: var(--neo-font-size-sm);
  font-weight: var(--neo-font-weight-medium);
  color: var(--neo-text-secondary);
}

.neo-input__label--required::after {
  content: '*';
  color: var(--neo-danger);
  margin-left: 4px;
}

.neo-input__wrapper {
  position: relative;
  display: flex;
  align-items: center;
}

.neo-input__field {
  width: 100%;
  height: var(--neo-input-height);
  padding: var(--neo-input-padding);
  font-size: var(--neo-font-size-md);
  color: var(--neo-text-primary);
  background: var(--neo-bg-surface-1);
  border: 1px solid var(--neo-border-default);
  border-radius: var(--neo-input-radius);
  transition: all var(--neo-duration-fast) var(--neo-ease-default);
  outline: none;
}

.neo-input__field::placeholder {
  color: var(--neo-text-tertiary);
}

.neo-input__field--has-icon {
  padding-left: 44px;
}

.neo-input__field:hover:not(:disabled) {
  border-color: var(--neo-border-strong);
}

.neo-input--focused .neo-input__field {
  border-color: var(--neo-primary);
  box-shadow: 0 0 0 3px var(--neo-primary-dim);
}

.neo-input--error .neo-input__field {
  border-color: var(--neo-danger);
}

.neo-input--error.neo-input--focused .neo-input__field {
  box-shadow: 0 0 0 3px var(--neo-danger-bg);
}

.neo-input--disabled .neo-input__field {
  opacity: 0.5;
  cursor: not-allowed;
  background: var(--neo-bg-surface-2);
}

.neo-input__icon {
  position: absolute;
  width: 18px;
  height: 18px;
  color: var(--neo-text-tertiary);
  pointer-events: none;
  stroke-width: 1.5;
}

.neo-input__icon--prefix {
  left: 14px;
}

.neo-input__icon--suffix {
  right: 14px;
}

.neo-input__clear {
  position: absolute;
  right: 0;
  width: 44px;
  height: 44px;
  padding: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  border: none;
  cursor: pointer;
  color: var(--neo-text-tertiary);
  transition: color var(--neo-duration-fast) var(--neo-ease-default);
}

/* 视觉圆圈保持 28px：hit 区 44px 透明 padding 扩展 */
.neo-input__clear::before {
  content: '';
  position: absolute;
  width: 28px;
  height: 28px;
  border-radius: 50%;
  background: var(--neo-bg-surface-2);
  transition: background var(--neo-duration-fast) var(--neo-ease-default);
}

.neo-input__clear:hover {
  color: var(--neo-text-primary);
}

.neo-input__clear:hover::before {
  background: var(--neo-bg-surface-3);
}

.neo-input__clear svg {
  position: relative;
  z-index: 1;
  width: 14px;
  height: 14px;
}

.neo-input__message {
  display: flex;
  align-items: center;
  min-height: 18px;
}

.neo-input__error,
.neo-input__hint {
  font-size: var(--neo-font-size-sm);
  line-height: 18px;
}

.neo-input__error {
  color: var(--neo-danger);
}

.neo-input__hint {
  color: var(--neo-text-tertiary);
}

/* 减少动画 */
@media (prefers-reduced-motion: reduce) {
  .neo-input__field {
    transition: none;
  }
}
</style>
