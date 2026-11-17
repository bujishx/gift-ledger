<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue';
import { api, queryString } from '../api';
import SearchToolbar from '../components/SearchToolbar.vue';
import MultiSelect from '../components/MultiSelect.vue';
import DateBasisSelect from '../components/DateBasisSelect.vue';
import StatisticsResults from '../components/StatisticsResults.vue';
import { periodOptions, periodRange, ledgerStatisticsParams, dateText, statisticsTabs, statisticsDrillParams } from '../lib/statisticsView.js';
import { statisticsViewState } from '../lib/statisticsSession.js';
import { GIFT_SEARCH_SCOPE } from '../lib/ledgerView.js';
import './stats-page.css';
const props = defineProps({ contacts: { type: Array, default: () => [] }, dictionaries: Object, viewState: Object });
const emit = defineEmits(['drill', 'update:view-state']);
const saved = statisticsViewState(props.viewState);
const tab = ref(saved.tab), preset = ref(saved.preset), year = ref(saved.year), month = ref(saved.month);
const filterOpen = ref(false), searchDraft = ref(saved.keyword), chartGrain = ref(saved.grain);
const loading = ref(false), error = ref(''), validationError = ref(''), data = ref(null), applied = ref(null);
const dateDraft = reactive({ date_from: saved.date_from, date_to: saved.date_to });
const filters = reactive({ keyword: saved.keyword, counterparty_relationship_type_ids: saved.counterparty_relationship_type_ids,
 event_type_ids: saved.event_type_ids, interaction_statuses: saved.interaction_statuses, date_basis: saved.date_basis,
 date_from: saved.date_from, date_to: saved.date_to });
const statusOptions = [{ value: 'self_unreturned', label: '我未回礼' }, { value: 'counterparty_unreturned', label: '对方未回礼' }, { value: 'mutual', label: '礼尚往来' }, { value: 'none', label: '暂无往来' }];
const relationOptions = computed(() => (props.dictionaries?.relationship || []).filter(item => item.id !== 1 && item.name !== '我').map(item => ({ value: item.id, label: item.name })));
const typeOptions = computed(() => (props.dictionaries?.event || []).map(item => ({ value: item.id, label: item.name })));
const rangeLabel = computed(() => applied.value?.date_from ? `${applied.value.date_from} 至 ${applied.value.date_to}` : '全部时间');
const chosenFilters = computed(() => [
 ...filters.counterparty_relationship_type_ids.map(value => ({ key: 'counterparty_relationship_type_ids', value, label: '往来关系：' + (relationOptions.value.find(item => item.value === value)?.label || value) })),
 ...filters.event_type_ids.map(value => ({ key: 'event_type_ids', value, label: '事件类型：' + (typeOptions.value.find(item => item.value === value)?.label || value) })),
 ...filters.interaction_statuses.map(value => ({ key: 'interaction_statuses', value, label: '来往状态：' + (statusOptions.find(item => item.value === value)?.label || value) })),
 ...(filters.date_basis === 'registered' ? [{ key: 'date_basis', value: 'registered', label: '日期口径：登记日期' }] : []),
]);
let request = 0, searchTimer;
function viewSnapshot() { return { preset: preset.value, year: year.value, month: month.value, ...filters, tab: tab.value, grain: chartGrain.value }; }
function applyPeriod() {
 try {
  const range = periodRange(preset.value, { year: year.value, month: month.value, from: dateDraft.date_from, to: dateDraft.date_to });
  Object.assign(dateDraft, range); Object.assign(filters, range); validationError.value = '';
 } catch (err) { request++; loading.value = false; validationError.value = err.message; }
}
function dateChanged(key, value) { dateDraft[key] = value; preset.value = 'custom'; applyPeriod(); }
async function load() {
 if (validationError.value) return;
 const token = ++request;
 const params = { ...filters, counterparty_relationship_type_ids: [...filters.counterparty_relationship_type_ids], event_type_ids: [...filters.event_type_ids], interaction_statuses: [...filters.interaction_statuses], grain: chartGrain.value };
 loading.value = true; error.value = '';
 try {
  const result = await api('/statistics/dashboard' + queryString(params));
  if (token === request) { data.value = result; applied.value = params; }
 } catch (err) { if (token === request) error.value = err.message; }
 finally { if (token === request) loading.value = false; }
}
function search() { clearTimeout(searchTimer); filters.keyword = searchDraft.value.trim(); }
watch(searchDraft, () => { clearTimeout(searchTimer); searchTimer = setTimeout(search, 250); });
function removeFilter(filter) {
 if (filter.key === 'date_basis') filters.date_basis = 'event';
 else filters[filter.key] = filters[filter.key].filter(value => value !== filter.value);
}
function clearModalFilters() { Object.assign(filters, { counterparty_relationship_type_ids: [], event_type_ids: [], interaction_statuses: [], date_basis: 'event' }); }
function clearAll() {
 clearTimeout(searchTimer); searchDraft.value = ''; filters.keyword = ''; clearModalFilters();
 preset.value = 'all'; year.value = new Date().getFullYear(); month.value = dateText(new Date()).slice(0, 7);
 chartGrain.value = 'auto'; applyPeriod();
}
function drill(extra, label) {
 if (!applied.value || loading.value || error.value || validationError.value) return;
 const params = ledgerStatisticsParams(statisticsDrillParams(applied.value, extra));
 const range = params.date_from ? `${params.date_from} 至 ${params.date_to}` : '全部时间';
 emit('drill', { params, label: `${label} · ${range}` });
}
applyPeriod();
watch([filters, chartGrain], load, { deep: true });
watch([filters, preset, year, month, tab, chartGrain], () => emit('update:view-state', viewSnapshot()), { deep: true, flush: 'sync' });
onMounted(load);
onUnmounted(() => { request++; clearTimeout(searchTimer); });
</script>
<template>
 <section class="statistics-page">
  <div class="page-heading"><div><h1>统计</h1><p>了解收支变化、事件开支和关系往来</p></div></div>
  <div class="card statistics-controls">
   <div class="stats-period-row">
    <label class="field">时间段<select v-model="preset" @change="applyPeriod"><option v-for="option in periodOptions" :key="option.id" :value="option.id">{{ option.label }}</option></select></label>
    <label v-if="preset === 'year'" class="field">年份<input v-model="year" type="number" min="1000" max="9999" @change="applyPeriod" /></label>
    <label v-if="preset === 'selectedMonth'" class="field">月份<input v-model="month" type="month" min="1000-01" max="9999-12" @change="applyPeriod" /></label>
    <div class="stats-date-range custom-visible"><label class="field">开始日期<input :value="dateDraft.date_from" type="date" min="1000-01-01" @change="dateChanged('date_from', $event.target.value)" /></label><span>至</span><label class="field">结束日期<input :value="dateDraft.date_to" type="date" min="1000-01-01" @change="dateChanged('date_to', $event.target.value)" /></label></div>
   </div>
   <SearchToolbar v-model="searchDraft" v-model:filter-open="filterOpen" modal-filters :filters="chosenFilters" :result-count="data?.summary?.gift_count ?? null" result-unit="笔" :validation-error="validationError" :loading="loading" :error="error" :scope="GIFT_SEARCH_SCOPE" @search="search" @clear="clearAll" @remove-filter="removeFilter" @clear-filters="clearModalFilters">
    <MultiSelect v-model="filters.counterparty_relationship_type_ids" label="往来人与我的关系" :options="relationOptions" all-label="全部关系" />
    <MultiSelect v-model="filters.event_type_ids" label="事件类型" :options="typeOptions" all-label="全部类型" />
    <MultiSelect v-model="filters.interaction_statuses" label="来往状态（全部历史）" :options="statusOptions" all-label="全部状态" />
    <DateBasisSelect v-model="filters.date_basis" />
   </SearchToolbar>
  </div>
  <p v-if="validationError" class="alert error" role="alert">{{ validationError }}<span v-if="data"> · 下方保留上次有效范围的结果（{{ rangeLabel }}）</span></p>
  <p v-if="error" class="alert error" role="alert">{{ error }}<span v-if="data"> · 下方保留上次成功应用的结果（{{ rangeLabel }}）</span><button @click="load">重试</button></p>
  <p v-if="loading" class="loading-state" role="status">正在计算统计，请稍候…</p>
  <div v-if="data" :inert="loading || !!error || !!validationError" :aria-busy="loading" class="statistics-results">
   <StatisticsResults :data="data" :tab="tab" :range-label="rangeLabel" :date-basis="applied.date_basis" :interaction-disabled="loading || !!error || !!validationError" @drill="drill" @grain-change="chartGrain = $event">
    <template #classification><nav class="subpage-tabs" role="tablist" aria-label="统计分类"><button v-for="item in statisticsTabs" :key="item.id" role="tab" :aria-selected="tab === item.id" :class="{ active: tab === item.id }" @click="tab = item.id">{{ item.label }}</button></nav></template>
   </StatisticsResults>
  </div>
  <nav v-else class="subpage-tabs" role="tablist" aria-label="统计分类"><button v-for="item in statisticsTabs" :key="item.id" role="tab" :aria-selected="tab === item.id" :class="{ active: tab === item.id }" @click="tab = item.id">{{ item.label }}</button></nav>
 </section>
</template>
