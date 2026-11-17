<script setup>
defineProps({ filters: { type: Array, default: () => [] } });
const emit = defineEmits(['remove']);
</script>
<template>
  <div v-if="filters.length" class="filter-chips" aria-label="已选筛选条件" aria-live="polite">
    <button v-for="filter in filters" :key="`${filter.key}:${filter.value ?? ''}`" type="button" class="filter-chip" :aria-label="`取消${filter.label}`" @click="emit('remove', filter)">
      <span>{{ filter.label }}</span><span aria-hidden="true">×</span>
    </button>
  </div>
</template>
