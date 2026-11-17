<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';
import { resizeRemarkTextarea } from '../lib/remarkTextarea.js';

const props = defineProps({
  modelValue: { type: String, default: '' },
  maxRows: { type: Number, default: 3 },
});
const emit = defineEmits(['update:modelValue']);
const textarea = ref(null);
const value = computed({
  get: () => props.modelValue ?? '',
  set: text => emit('update:modelValue', text),
});
let observer, measuredWidth;
const resize = () => resizeRemarkTextarea(textarea.value, props.maxRows);

watch([() => props.modelValue, () => props.maxRows], resize, { flush: 'post' });
onMounted(async () => {
  await nextTick();
  if (!textarea.value) return;
  resize();
  if (typeof ResizeObserver !== 'undefined') {
    observer = new ResizeObserver(() => {
      const width = textarea.value?.getBoundingClientRect().width;
      if (width === undefined || width === measuredWidth) return;
      measuredWidth = width;
      resize();
    });
    observer.observe(textarea.value, { box: 'border-box' });
  }
  window.addEventListener('resize', resize);
});
onUnmounted(() => {
  observer?.disconnect();
  window.removeEventListener('resize', resize);
});
</script>

<template>
  <textarea ref="textarea" v-model="value" class="remark-input" rows="1" @input="resize" />
</template>
