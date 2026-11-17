<script setup>
import { computed, ref } from "vue";

/**
 * 金额输入：数字后显示“元”。
 *
 * 输入框是“单向绑定 + input 回传”，组件从不回写 value，
 * 因此正在输入的数字不会被改写，光标也不会跳位。
 */
const props = defineProps({
  modelValue: { type: [String, Number], default: "" },
  placeholder: { type: String, default: "" },
  ariaLabel: { type: String, default: "" },
  inputId: { type: String, default: "" },
});
const emit = defineEmits(["update:modelValue"]);
const inputElement = ref(null);

const hasValue = computed(() => String(props.modelValue ?? "").trim() !== "");

function onInput(event) {
  emit("update:modelValue", event.target.value);
}
</script>

<template>
  <span class="amount-control" @click="inputElement?.focus()">
    <input
      ref="inputElement"
      :id="inputId || undefined"
      :value="modelValue"
      type="number"
      min="0"
      step="0.01"
      inputmode="decimal"
      autocomplete="off"
      :placeholder="placeholder"
      :aria-label="ariaLabel || undefined"
      @input="onInput"
    /><span v-if="hasValue" class="amount-unit" aria-hidden="true">元</span>
  </span>
</template>
