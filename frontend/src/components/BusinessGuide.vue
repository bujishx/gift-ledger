<script setup>
import { computed } from 'vue';
import ModalShell from './ModalShell.vue';
const props = defineProps({ page: String });
const emit = defineEmits(['close', 'navigate']);
const steps = [
  { id:'people',label:'添加联系人',description:'先登记一位往来的联系人，后续可以重复选择。' },
  { id:'events',label:'添加事件',description:'选择事主，填写亲属称谓、事件类型和办事日期。' },
  { id:'ledger',label:'记一笔账',description:'选择已有事件，再填写随礼人、礼金或实物。' },
];
const current = computed(() => steps.findIndex(step => step.id === props.page));
</script>
<template>
  <ModalShell family title="记账指南" class="business-guide" @close="emit('close')">
    <div class="accounting-guide"><strong>开始您的第一笔记账</strong>
      <ol class="guide-steps"><li v-for="(step,index) in steps" :key="step.id">
        <button :class="{current:index === current}" :aria-current="index === current ? 'step' : undefined" @click="emit('navigate',step.id)">
          <b>{{ index + 1 }}</b><span class="guide-step-copy"><strong>{{ step.label }}</strong><small>{{ step.description }}</small></span>
        </button>
      </li></ol>
      <p class="muted">已有联系人或事件，可以直接使用。</p>
    </div>
    <template #footer><button class="outline" @click="emit('close')">关闭</button></template>
  </ModalShell>
</template>
