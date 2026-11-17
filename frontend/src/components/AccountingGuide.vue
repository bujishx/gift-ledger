<script setup>
import { computed, ref } from 'vue';
const props = defineProps({ page: String, userId: [Number, String] });
const emit = defineEmits(['navigate', 'next']);
const steps = [{id:'people',label:'添加联系人'}, {id:'events',label:'添加事件'}, {id:'ledger',label:'记一笔账'}];
const key = `gift-ledger-guide-${props.userId}`;
const closed = ref(localStorage.getItem(key) === 'closed');
const current = computed(() => steps.findIndex(step => step.id === props.page));
const context = computed(() => ({people:'先登记一位往来的联系人，后续可以重复选择。',events:'选择已建立的联系人，登记谁家办什么事、哪天办。',ledger:'选择已有事件，再填写随礼人、礼金或礼物。'})[props.page]);
function dismiss() { closed.value = true; localStorage.setItem(key, 'closed'); }
function reopen() { closed.value = false; localStorage.removeItem(key); }
</script>
<template>
  <div v-if="!closed" class="accounting-guide card">
    <div class="guide-heading"><strong>开始您的第一笔记账</strong><button class="icon-button" aria-label="收起记账引导" @click="dismiss">×</button></div>
    <ol class="guide-steps"><li v-for="(step,index) in steps" :key="step.id"><button :class="{current:index === current}" :aria-current="index === current ? 'step' : undefined" @click="emit('navigate',step.id)"><b>{{index + 1}}</b>{{step.label}}</button><span v-if="index < 2" aria-hidden="true">→</span></li></ol>
    <p>{{context}}</p><div class="guide-footer"><button class="soft-primary" @click="emit('next')">{{steps[current]?.label}}</button><small>已有联系人或事件，可以直接使用。</small></div>
  </div>
  <button v-else class="guide-reopen text-button" @click="reopen">记账指南</button>
</template>
