<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';
import { statisticsTab } from '../lib/statisticsView.js';
import { money, signedMoney, balanceClass } from '../lib/format.js';
import './statistics-charts.css';

const props = defineProps({ data: Object, tab: String, rangeLabel: String, dateBasis: String, interactionDisabled: Boolean });
const emit = defineEmits(['drill', 'grain-change']);
const activeTab = computed(() => statisticsTab(props.tab));
const metricItems = computed(() => [
  { key: 'expense_amount', count: 'expense_count', label: '我随出', tone: 'expense' },
  { key: 'income_amount', count: 'income_count', label: '我收到', tone: 'income' },
  { key: 'net_amount', label: '收支差额', tone: balanceClass(props.data.summary.net_amount) },
]);
const directions = [{ key: 'expense', label: '随出' }, { key: 'income', label: '收到' }];
const grainOptions = [{ value: 'year', label: '每年' }, { value: 'quarter', label: '每季度' }, { value: 'month', label: '每月' }];
const analysisRows = computed(() => activeTab.value === 'events' ? props.data.event_types || [] : props.data.relationships || []);
const analysisMax = computed(() => analysisRows.value.reduce((maximum, row) => Math.max(maximum, Number(row.expense_amount), Number(row.income_amount)), 1));
const analysisAmountReserve = computed(() => Math.max(88, analysisRows.value.reduce((width, row) => Math.max(width, money(row.expense_amount).length, money(row.income_amount).length), 0) * 8 + 16) + 'px');
const analysisKey = computed(() => activeTab.value === 'events' ? 'event_type_id' : 'relationship_type_id');
const trendRows = computed(() => props.data.trends || []);
const trendColumnWidth = computed(() => props.data.grain === 'quarter' ? 108 : 84);
const plotHeight = 220;
const trendScale = computed(() => {
  const max = trendRows.value.reduce((maximum, row) => Math.max(maximum, Number(row.expense_amount), Number(row.income_amount)), 1);
  const rawStep = max / 4;
  const magnitude = 10 ** Math.floor(Math.log10(rawStep));
  const step = [1, 2, 2.5, 5, 10].find(value => value * magnitude >= rawStep) * magnitude;
  const ceiling = Math.ceil(max / step) * step;
  const intervals = Math.ceil(ceiling / step);
  return { ceiling, ticks: Array.from({ length: intervals + 1 }, (_, index) => ceiling - index * step) };
});
const activeKey = ref(null);
const tooltipRow = computed(() => trendRows.value.find(row => row.key === activeKey.value));
const chartTooltip = ref(null);
const tooltipPosition = ref({ left: '0px', top: '0px', visibility: 'hidden' });
let tooltipTrigger = null;
let tooltipMode = 'hover';
let tooltipHovered = false;
let tooltipAnchorY = null;
let closeTimer = null;
let focusFrame = null;
let lastPointerType = 'keyboard';
const trendScroll = ref(null);
const labelFor = row => row.label || (props.data.grain === 'year' ? `${row.key}年` : props.data.grain === 'quarter' ? `${row.key.slice(0, 4)}年第${row.key.slice(-1)}季度` : row.key);
const axisLabel = value => Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 2 });
const hasRows = (row, direction) => Number(row[direction ? `${direction}_count` : 'gift_count']) > 0;
const explanation = row => activeTab.value === 'events'
  ? `${row.event_count || 0} 个事件 · ${row.gift_count || 0} 笔记账 · 含实物 ${row.goods_count || 0} 笔`
  : `${row.people_count || 0} 人 · ${row.gift_count || 0} 笔记账 · 含实物 ${row.goods_count || 0} 笔`;
function analysisDrill(row, direction) {
  if (!hasRows(row, direction)) return;
  emit('drill', { [analysisKey.value]: row.id, ...(direction ? { direction } : {}) }, `${row.name}${direction ? ' · ' + (direction === 'expense' ? '随出' : '收到') : ''}`);
}
function cancelTooltipClose() {
  clearTimeout(closeTimer);
  closeTimer = null;
}
function cancelPendingFocus() {
  if (focusFrame !== null) cancelAnimationFrame(focusFrame);
  focusFrame = null;
}
function closeTooltip(cancelFocus = true) {
  if (cancelFocus) cancelPendingFocus();
  cancelTooltipClose();
  activeKey.value = null;
  tooltipHovered = false;
  tooltipTrigger = null;
}
function positionTooltip() {
  const tip = chartTooltip.value;
  const trigger = tooltipTrigger;
  if (!tip || !trigger?.isConnected) return;
  const bar = trigger.getBoundingClientRect();
  const bounds = tip.getBoundingClientRect();
  const viewportWidth = document.documentElement.clientWidth;
  const viewportHeight = window.innerHeight;
  const margin = 8;
  const gap = 10;
  const plotElement = trigger.closest('.cr-trend-bars');
  const visibleBars = Array.from(plotElement?.querySelectorAll('.cr-trend-bar') || [])
    .map(element => element.getBoundingClientRect()).filter(rect => rect.height > 0);
  const groupLeft = visibleBars.length ? Math.min(...visibleBars.map(rect => rect.left)) : bar.left;
  const groupRight = visibleBars.length ? Math.max(...visibleBars.map(rect => rect.right)) : bar.right;
  const bottomNav = document.querySelector('.bottom-nav');
  const navRect = bottomNav?.getBoundingClientRect();
  const navStyle = bottomNav && getComputedStyle(bottomNav);
  const navVisible = navStyle?.position === 'fixed' && navStyle.display !== 'none' && navStyle.visibility !== 'hidden'
    && navRect.height > 0 && navRect.top < viewportHeight && navRect.bottom > 0;
  const safeBottom = navVisible ? Math.min(viewportHeight - margin, navRect.top - margin) : viewportHeight - margin;
  const centerY = Math.max(bar.top, Math.min(bar.bottom, tooltipAnchorY ?? (bar.top + bar.height / 2)));
  let left;
  let top;
  if (groupRight + gap + bounds.width <= viewportWidth - margin) {
    left = groupRight + gap;
    top = centerY - bounds.height / 2;
  } else if (groupLeft - gap - bounds.width >= margin) {
    left = groupLeft - gap - bounds.width;
    top = centerY - bounds.height / 2;
  } else {
    left = (groupLeft + groupRight) / 2 - bounds.width / 2;
    top = bar.top - gap - bounds.height >= margin
      ? bar.top - gap - bounds.height
      : bar.bottom + gap;
  }
  // Keep a short bar's floating tip above its time label so the label remains
  // an independent, unobstructed entry to the period's ledger records.
  const plot = plotElement?.getBoundingClientRect();
  if (plot) top = Math.min(top, plot.bottom - bounds.height - 4);
  tooltipPosition.value = {
    left: Math.max(margin, Math.min(left, viewportWidth - bounds.width - margin)) + 'px',
    top: Math.max(margin, Math.min(top, safeBottom - bounds.height)) + 'px',
    visibility: 'visible',
  };
}
async function showTooltip(row, event, mode = 'hover') {
  if (props.interactionDisabled) return;
  cancelTooltipClose();
  activeKey.value = row.key;
  tooltipTrigger = event.currentTarget;
  tooltipMode = mode;
  tooltipHovered = false;
  tooltipAnchorY = mode === 'hover' && typeof event.clientY === 'number' ? event.clientY : null;
  tooltipPosition.value = { ...tooltipPosition.value, visibility: 'hidden' };
  await nextTick();
  positionTooltip();
}
function hoverTooltip(row, event) {
  if (event.pointerType === 'touch') return;
  showTooltip(row, event, 'hover');
}
function focusTooltip(row, event) {
  if (lastPointerType !== 'keyboard') return;
  cancelPendingFocus();
  const trigger = event.currentTarget;
  // Keyboard navigation may first scroll the focused bar into view. Position
  // its new tip after that browser scroll, rather than opening and losing it.
  focusFrame = requestAnimationFrame(() => {
    focusFrame = null;
    if (document.activeElement === trigger && lastPointerType === 'keyboard') {
      showTooltip(row, { currentTarget: trigger }, 'focus');
    }
  });
}
function clickTooltip(row, event) {
  const isTouch = event.pointerType === 'touch' || lastPointerType === 'touch';
  showTooltip(row, event, isTouch ? 'touch' : event.detail === 0 ? 'focus' : 'hover');
}
function scheduleTooltipClose(event) {
  if (event?.pointerType === 'touch' || tooltipMode === 'touch') return;
  // A focused keyboard trigger and a hovered tip remain readable.
  if (tooltipMode === 'focus' && document.activeElement === tooltipTrigger) return;
  cancelTooltipClose();
  closeTimer = setTimeout(() => { if (!tooltipHovered) closeTooltip(); }, 180);
}
function blurTooltip(event) {
  cancelPendingFocus();
  scheduleTooltipClose(event);
}
function handleChartScroll() { closeTooltip(false); }
function enterTooltip(event) {
  if (event.pointerType === 'touch') return;
  tooltipHovered = true;
  cancelTooltipClose();
}
function leaveTooltip(event) {
  tooltipHovered = false;
  scheduleTooltipClose(event);
}
function handleOutsidePointer(event) {
  lastPointerType = event.pointerType || 'mouse';
  if (activeKey.value === null || chartTooltip.value?.contains(event.target) || event.target.closest?.('.cr-trend-bar')) return;
  closeTooltip();
}
function handleEscape(event) {
  if (event.key === 'Tab') lastPointerType = 'keyboard';
  if (event.key === 'Escape' && (activeKey.value !== null || focusFrame !== null)) {
    event.preventDefault(); closeTooltip();
  }
}
function trendDrill(row) {
  if (!hasRows(row)) return;
  closeTooltip();
  emit('drill', { date_from: row.date_from, date_to: row.date_to }, labelFor(row));
}
async function showLatestPeriod() {
  await nextTick();
  if (trendScroll.value) trendScroll.value.scrollLeft = trendScroll.value.scrollWidth;
}
watch([() => props.data, () => props.tab], () => { closeTooltip(); showLatestPeriod(); });
watch(() => props.interactionDisabled, disabled => { if (disabled) closeTooltip(); });
onMounted(() => {
  document.addEventListener('keydown', handleEscape);
  document.addEventListener('pointerdown', handleOutsidePointer, true);
  window.addEventListener('resize', closeTooltip);
  window.addEventListener('scroll', handleChartScroll, true);
  showLatestPeriod();
});
onUnmounted(() => {
  cancelTooltipClose();
  cancelPendingFocus();
  document.removeEventListener('keydown', handleEscape);
  document.removeEventListener('pointerdown', handleOutsidePointer, true);
  window.removeEventListener('resize', closeTooltip);
  window.removeEventListener('scroll', handleChartScroll, true);
});
</script>

<template>
  <div class="stat-summary-grid">
    <button v-for="metric in metricItems" :key="metric.key" class="card stat-summary" :disabled="!hasRows(data.summary, metric.key === 'net_amount' ? null : metric.tone)" @click="emit('drill', metric.key === 'net_amount' ? {} : { direction: metric.tone }, metric.label)">
      <small>{{ metric.label }}</small>
      <strong :class="metric.tone">{{ metric.key === 'net_amount' ? signedMoney(data.summary[metric.key]) : money(data.summary[metric.key]) }}</strong>
      <span>{{ metric.count ? data.summary[metric.count] + ' 笔' : '收入 − 支出' }}</span>
    </button>
  </div>
  <p class="stats-caption">{{ rangeLabel }} · {{ dateBasis === 'registered' ? '按登记日期统计录入情况' : '按办事日期统计' }} · {{ data.summary.people_count }} 位联系人 · {{ data.summary.event_count }} 个事件 · {{ data.summary.gift_count }} 笔记账记录 · 含实物 {{ data.summary.goods_count }} 笔（纯实物 {{ data.summary.pure_goods_count }} 笔，金额不估价）</p>
  <p v-if="data.date_information_insufficient?.gift_count" class="card stats-date-information" role="status">另有 {{ data.date_information_insufficient.gift_count }} 笔旧账因日期精度不足，无法确定是否归属所选期间，未计入本期间汇总：收入 {{ money(data.date_information_insufficient.income_amount) }}，支出 {{ money(data.date_information_insufficient.expense_amount) }}，其中纯实物 {{ data.date_information_insufficient.pure_goods_count || 0 }} 笔。切换“全部时间”可纳入核对。</p>
  <slot name="classification" />
  <p v-if="!data.summary.gift_count" class="empty">所选时间段和条件下没有记账记录。可以扩大时间范围或清除条件。</p>

  <div v-if="activeTab === 'overview'" class="card stat-panel cr-trend-panel">
    <div class="stat-panel-heading">
      <h2>收支趋势</h2>
      <span class="chart-legend"><i class="expense-bg"></i>随出 <i class="income-bg"></i>收到</span>
    </div>
    <div class="cr-grain-controls">
      <span id="chart-grain-label">图表单位</span>
      <div class="cr-grain-options" role="group" aria-label="图表单位" aria-labelledby="chart-grain-label">
        <button v-for="item in grainOptions" :key="item.value" type="button" :aria-pressed="data.grain === item.value" :class="{ active: data.grain === item.value }" @click="emit('grain-change', item.value)">{{ item.label }}</button>
      </div>
    </div>
    <p class="cr-chart-range muted">统计范围：{{ rangeLabel }}。跨年或季度只计入此范围内的记录。</p>
    <div v-if="trendRows.length" class="cr-chart-frame">
      <div class="cr-y-axis" aria-hidden="true">
        <small class="cr-axis-title">金额（元）</small>
        <div class="cr-axis-scale" :style="{ height: plotHeight + 'px' }">
          <span v-for="tick in trendScale.ticks" :key="tick" :style="{ top: (1 - tick / trendScale.ceiling) * 100 + '%' }">{{ axisLabel(tick) }}</span>
        </div>
      </div>
      <div ref="trendScroll" class="cr-trend-scroll" tabindex="0" aria-label="收支趋势，可横向滚动；聚焦柱子查看金额，点击时间标签查看记账记录" @scroll="handleChartScroll">
        <div class="cr-trend-canvas" :style="{ minWidth: Math.max(trendRows.length * trendColumnWidth, 180) + 'px', '--trend-column-width': trendColumnWidth + 'px' }">
          <div class="cr-trend-grid" :style="{ height: plotHeight + 'px' }" aria-hidden="true">
            <i v-for="tick in trendScale.ticks" :key="tick" :class="{ baseline: tick === 0 }" :style="{ top: (1 - tick / trendScale.ceiling) * 100 + '%' }"></i>
          </div>
          <div v-for="row in trendRows" :key="row.key" class="cr-trend-column trend-column" :data-bucket="row.key" :class="{ selected: activeKey === row.key }">
            <span class="cr-trend-bars" :style="{ height: plotHeight + 'px' }">
              <button v-for="direction in directions" :key="direction.key" type="button" class="cr-trend-bar" :class="direction.key + '-bg'" :data-direction="direction.key" :data-bucket="row.key" :style="{ height: Number(row[direction.key + '_amount']) / trendScale.ceiling * plotHeight + 'px' }" :disabled="Number(row[direction.key + '_amount']) <= 0" :tabindex="Number(row[direction.key + '_amount']) > 0 ? 0 : -1" :aria-label="`${labelFor(row)}，${direction.label}${money(row[direction.key + '_amount'])}，显示收支金额`" :aria-describedby="activeKey === row.key ? 'chart-period-tooltip' : undefined" @pointerenter="hoverTooltip(row, $event)" @pointerleave="scheduleTooltipClose($event)" @focus="focusTooltip(row, $event)" @blur="blurTooltip($event)" @click="clickTooltip(row, $event)"></button>
            </span>
            <button type="button" class="cr-time-label" :data-bucket="row.key" :disabled="!hasRows(row)" :aria-label="`${labelFor(row)}，查看记账记录`" @click="trendDrill(row)">{{ labelFor(row) }}</button>
          </div>
        </div>
      </div>
    </div>
    <p v-else class="muted cr-no-trend">当前记录没有可归入此图表单位的日期。</p>
    <p class="muted cr-chart-help">悬停、键盘聚焦或轻点柱子查看收支金额；点击时间标签查看该时段的记账记录。</p>
    <Teleport to="body">
      <div v-if="tooltipRow" id="chart-period-tooltip" ref="chartTooltip" class="cr-trend-tooltip" role="tooltip" :style="tooltipPosition" @pointerenter="enterTooltip" @pointerleave="leaveTooltip">
        <p>随出：<strong class="expense">{{ money(tooltipRow.expense_amount) }}</strong></p>
        <p>收入：<strong class="income">{{ money(tooltipRow.income_amount) }}</strong></p>
        <p>差额：<strong :class="balanceClass(Number(tooltipRow.income_amount) - Number(tooltipRow.expense_amount))">{{ signedMoney(Number(tooltipRow.income_amount) - Number(tooltipRow.expense_amount)) }}</strong></p>
      </div>
    </Teleport>
    <p v-if="data.trend_date_information_insufficient?.gift_count" class="muted cr-date-note" role="status">总览中另有 {{ data.trend_date_information_insufficient.gift_count }} 笔旧账因日期精度不足，无法放入当前趋势时间段：收入 {{ money(data.trend_date_information_insufficient.income_amount) }}，支出 {{ money(data.trend_date_information_insufficient.expense_amount) }}。这些记录仍计入上方总览，趋势图不猜测日期。</p>
  </div>

  <div v-else class="card stat-panel cr-analysis-panel" :style="{ '--cr-amount-reserve': analysisAmountReserve }">
    <div class="stat-panel-heading"><h2>{{ activeTab === 'events' ? '各事件类型收支金额' : '关系往来' }}</h2><span class="chart-legend"><i class="expense-bg"></i>随出 <i class="income-bg"></i>收到</span></div>
    <article v-for="row in analysisRows" :key="row.id" class="cr-analysis-row relationship-chart-row" :class="{ 'event-type-chart-row': activeTab === 'events' }">
      <div class="cr-analysis-heading">
        <button type="button" class="cr-category-button" :disabled="!hasRows(row)" :aria-label="`${row.name}，查看全部${row.gift_count}笔记账`" @click="analysisDrill(row)">{{ row.name }}</button>
        <small>{{ explanation(row) }}</small>
      </div>
      <div v-for="direction in directions" :key="direction.key" class="cr-analysis-bar">
        <small>{{ direction.label }}</small>
        <div class="cr-analysis-plot">
          <i :class="direction.key + '-bg'" :style="{ width: Number(row[direction.key + '_amount']) / analysisMax * 100 + '%' }" aria-hidden="true"></i>
          <button type="button" class="cr-bar-amount" :class="direction.key" :style="{ left: Number(row[direction.key + '_amount']) / analysisMax * 100 + '%' }" :disabled="!hasRows(row, direction.key)" :aria-label="`${row.name}，${direction.label}${money(row[direction.key + '_amount'])}，${row[direction.key + '_count']}笔记账${hasRows(row, direction.key) ? '，查看记录' : '，没有记录'}`" @click="analysisDrill(row, direction.key)">{{ money(row[direction.key + '_amount']) }}</button>
        </div>
      </div>
    </article>
    <p v-if="!analysisRows.length" class="muted">{{ activeTab === 'events' ? '没有符合条件的事件类型。' : '没有符合条件的关系往来。' }}</p>
    <p class="muted cr-chart-help">点击分类名称查看全部记录，点击随出或收到金额查看对应记录。金额只统计礼金，实物不折算金额。</p>
  </div>
</template>
