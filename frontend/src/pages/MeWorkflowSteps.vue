<script setup>
/* “我的”页面导入／恢复共用的阶段条（U11）。
   只做展示：阶段数据由 MePage 的纯函数计算后传入。 */
defineProps({
  steps: { type: Array, required: true },
  label: { type: String, default: "操作阶段" },
  busy: { type: Boolean, default: false },
});
</script>

<template>
  <ol class="step-track me-steps" :aria-label="label">
    <li
      v-for="step in steps"
      :key="step.label"
      :class="step.state"
      :aria-current="step.state === 'current' ? 'step' : undefined"
    >
      <span>{{ step.label }}</span>
      <em v-if="busy && step.state === 'current'" class="me-step-busy"
        >处理中…</em
      >
    </li>
  </ol>
</template>
