<script setup lang="ts">
import Modal from '@/components/ui/Modal.vue'

defineProps<{
  visible: boolean
  deviceName: string
}>()

defineEmits<{
  (e: 'confirm'): void
  (e: 'cancel'): void
}>()
</script>

<template>
  <!-- 结束播放二次确认：这是踢掉其他设备的操作，对方会立即断开 -->
  <Modal :model-value="visible" title="结束播放" size="sm" @update:model-value="(v) => { if (!v) $emit('cancel') }">
    <p class="stop-confirm-text">
      确定要结束「{{ deviceName || '未知设备' }}」上的播放吗？对方将立即断开连接。
    </p>
    <template #footer>
      <div class="stop-confirm-actions">
        <button class="au-btn au-btn-ghost au-btn-sm" @click="$emit('cancel')">取消</button>
        <button class="au-btn au-btn-danger au-btn-sm" @click="$emit('confirm')">确定结束</button>
      </div>
    </template>
  </Modal>
</template>

<style scoped>
/* 从 HomeView 整体迁移（template + script + style，包一层 ui/Modal） */
.stop-confirm-text {
  margin: 0;
  font-size: 0.875rem;
  line-height: 1.6;
  color: var(--au-text-2);
}

.stop-confirm-actions {
  display: flex;
  justify-content: flex-end;
  gap: 0.5rem;
}
</style>
