<script setup>
import { computed } from 'vue';
import { useTheme } from '../lib/theme.js';
const { effectiveTheme, toggleTheme } = useTheme();
const label = computed(() => effectiveTheme.value === 'dark' ? '当前深色，切换为浅色' : '当前浅色，切换为深色');
</script>

<template>
  <button type="button" class="outline theme-trigger" :aria-label="label" :title="label" @click="toggleTheme">
    <svg v-if="effectiveTheme === 'light'" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/></svg>
    <svg v-else viewBox="0 0 24 24" aria-hidden="true"><path d="M20.5 14.5A9 9 0 0 1 9.5 3.5 9 9 0 1 0 20.5 14.5Z"/></svg>
  </button>
</template>
