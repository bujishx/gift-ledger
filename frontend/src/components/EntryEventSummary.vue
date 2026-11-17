<script setup>
import { eventDate } from '../lib/format.js';
defineProps({
  detail: { type: Object, default: null },
  formatTitle: { type: Function, required: true },
  formatStamp: { type: Function, required: true },
});
</script>

<template>
  <div v-if="detail" class="entry-summary entry-event-summary" role="status">
    <div class="summary-heading"><span class="summary-check" aria-hidden="true">✓</span><strong>已选记账事件</strong></div>
    <p class="summary-event-title">{{ formatTitle(detail) }}</p>
    <div class="summary-dates"><span>办事日期 {{ eventDate(detail) }}</span><span>登记 {{ formatStamp(detail.event.created_at) }}</span></div>
    <p class="summary-next">事件已选择，请填写礼金或礼物。</p>
  </div>
</template>

<style scoped>
.entry-event-summary { min-width: 0; padding: 15px 16px; margin-bottom: 12px; border: 2px solid #0b57d0; border-radius: 14px; background: #edf4ff; }
.summary-heading { display: flex; align-items: center; gap: 8px; color: #16487d; font-size: 14px; }
.summary-heading strong { margin: 0; }
.summary-check { display: inline-flex; flex: none; align-items: center; justify-content: center; width: 23px; height: 23px; border-radius: 50%; background: #0b57d0; color: white; font-weight: 700; }
.summary-event-title { margin: 10px 0 8px; color: #172f4d; font-size: 14px; font-weight: 700; line-height: 1.65; overflow-wrap: anywhere; }
.summary-dates { display: flex; flex-wrap: wrap; gap: 4px 12px; color: #61738d; font-size: 12px; line-height: 1.7; }
.summary-next { margin: 10px 0 0; padding-top: 9px; border-top: 1px solid #c5d8f3; color: #16487d; font-size: 12px; line-height: 1.7; }
@media (max-width:700px) { .entry-event-summary { padding: 13px 14px; } }
</style>
