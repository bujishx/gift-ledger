<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, useId, watch } from 'vue';
import UiIcon from './UiIcon.vue';
import { eventDate } from '../lib/format.js';

const props = defineProps({
  rows: { type: Array, default: () => [] },
  selectedId: { type: [Number, String], default: null },
  selectedTitle: { type: String, default: '' },
  selectedDate: { type: String, default: '' },
  search: { type: String, default: '' },
  loading: Boolean,
  selecting: Boolean,
  busy: Boolean,
  formatTitle: { type: Function, required: true },
  formatStamp: { type: Function, required: true },
});
const emit = defineEmits(['update:search', 'choose']);
const headingId = useId(), listId = useId();
const phone = ref(false), expanded = ref(true), confirmation = ref(null);
const optionRefs = new Map();
const selected = computed(() => props.selectedId !== null && props.selectedId !== undefined && props.selectedId !== '');
const choicesVisible = computed(() => !phone.value || !selected.value || expanded.value);
const disabled = computed(() => props.loading || props.selecting || props.busy);
const currentIndex = computed(() => props.rows.findIndex(row => String(row.event.id) === String(props.selectedId)));

function setOptionRef(id, element) {
  if (element) optionRefs.set(String(id), element);
  else optionRefs.delete(String(id));
}
function checkWidth() { phone.value = window.innerWidth <= 700; }
function isSelected(id) { return selected.value && String(id) === String(props.selectedId); }
function tabIndex(index) { return index === (currentIndex.value >= 0 ? currentIndex.value : 0) ? 0 : -1; }
function choose(id) {
  if (disabled.value) return;
  if (phone.value && isSelected(id)) {
    expanded.value = false;
    nextTick(() => confirmation.value?.focus({ preventScroll: true }));
    return;
  }
  emit('choose', id);
}
function keySelect(event, index) {
  const movements = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 };
  if (disabled.value || !props.rows.length) return;
  let next;
  if (event.key === 'Home') next = 0;
  else if (event.key === 'End') next = props.rows.length - 1;
  else if (event.key in movements) next = (index + movements[event.key] + props.rows.length) % props.rows.length;
  else return;
  event.preventDefault();
  const id = props.rows[next].event.id;
  optionRefs.get(String(id))?.focus({ preventScroll: true });
  optionRefs.get(String(id))?.scrollIntoView({ block: 'nearest' });
  choose(id);
}
async function changeEvent() {
  if (disabled.value) return;
  expanded.value = true;
  // 重新展开时显示全部候选，确保当前选项不会被候选搜索遮住。
  emit('update:search', '');
  await nextTick();
  const option = optionRefs.get(String(props.selectedId));
  option?.scrollIntoView({ block: 'nearest' });
  option?.focus({ preventScroll: true });
}
watch([() => props.selectedId, phone], async () => {
  if (!selected.value) { expanded.value = true; return; }
  if (!phone.value) return;
  // 以父组件确认后的选中 ID 为准；切换取消或请求失败不会收起候选。
  expanded.value = false;
  await nextTick();
  confirmation.value?.focus({ preventScroll: true });
  confirmation.value?.scrollIntoView({ block: 'nearest' });
}, { flush: 'post' });
onMounted(() => { checkWidth(); window.addEventListener('resize', checkWidth); });
onUnmounted(() => { window.removeEventListener('resize', checkWidth); optionRefs.clear(); });
</script>

<template>
  <div class="entry-event-picker">
    <div v-if="phone && selected" ref="confirmation" class="selection-confirmation" :class="{ 'choices-expanded': expanded }" tabindex="-1" role="status">
      <div class="confirmation-heading">
        <strong><span class="confirmation-check" aria-hidden="true">✓</span> 事件已选择</strong>
        <button type="button" class="outline change-event" :disabled="disabled" :aria-expanded="expanded" :aria-controls="listId" @click="changeEvent">更换事件</button>
      </div>
      <p v-if="expanded" class="selected-event-title">{{ selectedTitle }}</p>
      <small v-if="expanded">办事日期 {{ selectedDate }}</small>
      <small v-if="expanded" class="changing-help">选择其他事件后才会更换；当前选择仍保留。</small>
    </div>
    <div v-if="choicesVisible" class="field picker-field">
      <span :id="headingId" class="field-label">选择已有事件 <span class="required-mark">*</span></span>
      <div class="search-wrap">
        <UiIcon name="search" />
        <input :value="search" placeholder="搜索已有事件" aria-label="搜索已有事件" :disabled="disabled" @input="emit('update:search', $event.target.value)" />
      </div>
      <div :id="listId" class="entry-event-list" role="radiogroup" :aria-labelledby="headingId" :aria-busy="loading || selecting">
        <button v-for="(row,index) in rows" :key="row.event.id" :ref="element => setOptionRef(row.event.id, element)"
          type="button" class="entry-event-choice" :class="{ selected: isSelected(row.event.id) }" role="radio"
          :aria-checked="isSelected(row.event.id)" :tabindex="tabIndex(index)" :disabled="disabled"
          @click="choose(row.event.id)" @keydown="keySelect($event,index)">
          <span class="choice-radio" :class="{ checked: isSelected(row.event.id) }" aria-hidden="true"><i v-if="isSelected(row.event.id)"></i></span>
          <span class="choice-content">
            <span class="choice-title-line"><strong>{{ formatTitle(row) }}</strong><span v-if="isSelected(row.event.id)" class="selected-badge">✓ 已选择</span></span>
            <span class="choice-details">办事日期 {{ eventDate(row) }} · {{ row.gift_count }} 笔记账记录 · 登记 {{ formatStamp(row.event.created_at) }}</span>
          </span>
        </button>
        <p v-if="!loading && !rows.length" class="empty-small">没有符合条件的事件，请先到事件页添加。</p>
      </div>
      <p v-if="!loading && !selected" class="picker-help">选中一项事件后，再填写礼金或礼物。</p>
    </div>
    <p v-if="loading || selecting" class="loading-state" role="status">{{ selecting ? '正在选择事件…' : '正在加载事件…' }}</p>
  </div>
</template>

<style scoped>
.entry-event-picker { min-width: 0; }
.picker-field { gap: 8px; }
.required-mark { color: #c8392b; }
.entry-event-list { border: 1px solid #cbd9ef; border-radius: 12px; overflow: auto; max-height: min(55dvh,580px); margin-top: 4px; }
.entry-event-list .entry-event-choice { display: flex; flex-direction: row; align-items: flex-start; gap: 10px; width: 100%; min-height: 74px; border: 0; border-bottom: 1px solid #e0e7f2; background: white; text-align: left; padding: 13px 12px; outline: 0; color: #172f4d; }
.entry-event-list .entry-event-choice:last-of-type { border-bottom: 0; }
.entry-event-list .entry-event-choice.selected { background: #e5efff; box-shadow: inset 4px 0 #0b57d0, inset 0 0 0 2px #0b57d0; }
.entry-event-list .entry-event-choice:focus-visible { outline: 2px solid #0b57d0; outline-offset: -4px; }
.entry-event-list .entry-event-choice:disabled { opacity: .75; cursor: wait; }
.entry-event-list .choice-radio { display: flex; flex: none; align-items: center; justify-content: center; width: 18px; height: 18px; margin-top: 1px; border: 2px solid #a0b3ce; border-radius: 50%; background: white; }
.entry-event-list .choice-radio.checked { border-color: #0b57d0; }
.choice-radio i { width: 8px; height: 8px; background: #0b57d0; border-radius: 50%; }
.entry-event-list .choice-content { min-width: 0; flex: 1; display: flex; flex-direction: column; gap: 7px; }
.entry-event-list .choice-title-line { display: flex; flex-wrap: wrap; align-items: flex-start; gap: 6px 8px; }
.entry-event-list .choice-title-line strong { min-width: 0; flex: 1 1 180px; overflow-wrap: anywhere; font-size: 13px; line-height: 1.55; color: #172f4d; }
.entry-event-list .selected-badge { flex: none; padding: 3px 7px; border-radius: 12px; background: #0b57d0; color: white; font-size: 11px; font-weight: 700; white-space: nowrap; }
.entry-event-list .choice-details { color: #5f718b; font-size: 12px; line-height: 1.7; overflow-wrap: anywhere; }
.picker-help { margin: 2px 0 0; font-size: 12px; line-height: 1.7; color: #65758d; }
.selection-confirmation { background: white; border: 0; border-bottom: 1px solid #dde5f2; border-radius: 0; padding: 8px 0 10px; margin-bottom: 12px; outline: none; }
.selection-confirmation.choices-expanded { border: 1px solid #cbd9ef; border-radius: 12px; padding: 12px; }
.selection-confirmation:focus-visible { outline: 2px solid #0b57d0; outline-offset: 3px; }
.confirmation-heading { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; }
.confirmation-heading strong { color: #16487d; font-size: 14px; }
.confirmation-check { display: inline-flex; justify-content: center; align-items: center; width: 21px; height: 21px; background: #0b57d0; border-radius: 50%; color: white; }
.change-event { min-height: 36px; padding: 7px 12px; font-size: 12px; background: white; }
.selected-event-title { margin: 9px 0 5px; line-height: 1.6; color: #172f4d; overflow-wrap: anywhere; font-size: 13px; font-weight: 700; }
.selection-confirmation small { display: block; color: #5f718b; line-height: 1.7; }
.selection-confirmation .changing-help { margin-top: 8px; color: #17457f; }
.empty-small { margin: 0; padding: 15px; }
@media (max-width:900px) { .entry-event-list { max-height: 230px; } }
</style>
