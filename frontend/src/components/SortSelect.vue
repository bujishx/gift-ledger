<script setup>
defineProps({ modelValue: String, direction: String, options: Array });
const emit = defineEmits(['update:modelValue','update:direction']);
</script>
<template>
  <div class="sort-control mobile-sort">
    <label>排序<select :value="modelValue" @change="emit('update:modelValue', $event.target.value)"><option v-for="item in options" :key="item.value" :value="item.value">{{item.label}}</option></select></label>
    <button class="text-button" :aria-label="direction === 'desc' ? '当前倒序，点击改正序' : '当前正序，点击改倒序'" @click="emit('update:direction', direction === 'desc' ? 'asc' : 'desc')">{{direction === 'desc' ? '倒序 ▼' : '正序 ▲'}}</button>
  </div>
</template>
