<script setup>
import ModalShell from './ModalShell.vue';
import { computed } from 'vue';
const props = defineProps({ state: Object });
const emit = defineEmits(['answer']);
const dangerous = computed(() => props.state.confirmTone === 'danger');
</script>
<template>
  <ModalShell v-if="state.open" family class="dialog-backdrop compact-family-dialog" :title="state.title" :closing-disabled="state.busy" @close="emit('answer',false)">
    <p class="muted dialog-message">{{state.message}}</p>
    <form id="ui-dialog-form" @submit.prevent="emit('answer',true)">
      <label v-if="state.input" class="field">{{state.inputLabel}}<input v-model="state.value" required maxlength="50" autocomplete="off" /></label>
      <p v-if="state.error" class="alert error" role="alert">{{state.error}}</p>
    </form>
    <template #footer><div class="form-actions"><button :class="dangerous ? 'primary' : 'outline'" :disabled="state.busy" @click="emit('answer',false)">{{state.cancelLabel}}</button><button form="ui-dialog-form" :class="dangerous ? 'danger-outline' : 'primary'" :disabled="state.busy">{{state.busy ? '正在处理…' : state.confirmLabel}}</button></div></template>
  </ModalShell>
</template>
