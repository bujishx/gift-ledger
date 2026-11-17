<script setup>
import { computed, useId } from 'vue';
import UiIcon from './UiIcon.vue';
import FilterChips from './FilterChips.vue';
import FilterDialog from './FilterDialog.vue';
const props = defineProps({ modelValue: String, filterOpen: Boolean, placeholder: String, scope: String, filters: { type: Array, default: () => [] }, modalFilters: Boolean, resultCount: { type: Number, default: null }, resultUnit: String, loading: Boolean, error: String, validationError: String });
const emit = defineEmits(['update:modelValue', 'update:filterOpen', 'search', 'clear', 'remove-filter', 'clear-filters']);
const hintId = useId();
const target = computed(() => (props.scope || '').split('；')[0]);
</script>
<template>
  <div class="card toolbar">
    <div class="toolbar-main">
      <div class="search-wrap">
        <UiIcon name="search" />
        <input :value="modelValue" placeholder="输入搜索关键字" aria-label="输入搜索关键字" :aria-describedby="hintId" @input="emit('update:modelValue', $event.target.value)" @keydown.enter.prevent="emit('search')" />
      </div>
      <button class="outline" type="button" :aria-expanded="filterOpen" :aria-haspopup="modalFilters ? 'dialog' : undefined" @click="emit('update:filterOpen', !filterOpen)">筛选</button>
      <button class="text-button" type="button" @click="emit('clear')">清除</button>
    </div>
    <p :id="hintId" class="search-help"><span>{{ target }}；</span><span>用空格分隔关键词，匹配其中任意一个</span></p>
    <div v-if="!modalFilters && filterOpen" class="filter-grid"><slot /></div>
    <FilterDialog v-if="modalFilters" :open="filterOpen" :filters="filters" :result-count="resultCount" :result-unit="resultUnit" :loading="loading" :error="error" :validation-error="validationError" @close="emit('update:filterOpen', false)" @remove-filter="emit('remove-filter', $event)" @clear-filters="emit('clear-filters')"><slot /></FilterDialog>
    <div v-if="modalFilters && filters.length" class="toolbar-filter-rail" aria-label="已选筛选条件">
      <span class="toolbar-filter-count">{{ filters.length }} 项条件</span>
      <FilterChips :filters="filters" @remove="emit('remove-filter', $event)" />
    </div>
    <FilterChips v-else-if="!modalFilters" :filters="filters" @remove="emit('remove-filter', $event)" />
  </div>
</template>
<style scoped>
.toolbar-filter-rail { display: flex; align-items: center; height: 60px; min-height: 60px; min-width: 0; gap: 10px; overflow: hidden; }
.toolbar-filter-count { flex-shrink: 0; color: #596b84; font-size: 12px; }
.toolbar-filter-rail :deep(.filter-chips) { flex: 1; min-width: 0; display: flex; flex-wrap: nowrap; align-items: center; height: 60px; margin: 0; overflow-x: auto; overflow-y: hidden; gap: 8px; scrollbar-width: thin; overscroll-behavior-x: contain; }
.toolbar-filter-rail :deep(.filter-chip) { flex-shrink: 0; max-width: none; white-space: nowrap; }
</style>
