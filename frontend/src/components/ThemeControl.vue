<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, useId } from 'vue';
import { useTheme } from '../lib/theme.js';
const { themeMode, effectiveTheme, setThemeMode } = useTheme();
const root = ref(null), trigger = ref(null), open = ref(false), menuId = useId();
const options = [{ id: 'system', label: '跟随系统' }, { id: 'light', label: '明亮' }, { id: 'dark', label: '黑暗' }];
const label = computed(() => options.find(option => option.id === themeMode.value)?.label || '跟随系统');
function close(returnFocus = false) { open.value = false; if (returnFocus) trigger.value?.focus({ preventScroll: true }); }
async function toggle() {
  open.value = !open.value;
  if (open.value) { await nextTick(); root.value?.querySelector('input:checked')?.focus({ preventScroll: true }); }
}
function choose(mode) { setThemeMode(mode); close(true); }
function radioClick(event, mode) { if (event.detail > 0) choose(mode); }
function key(event) {
  if (!open.value || !root.value?.contains(event.target)) return;
  if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); close(true); }
  if (event.key === 'Enter' && event.target.matches('input[type="radio"]')) { event.preventDefault(); choose(event.target.value); }
}
function outside(event) { if (open.value && !root.value?.contains(event.target)) close(); }
function focusOut(event) { if (open.value && !root.value?.contains(event.relatedTarget)) close(); }
onMounted(() => { document.addEventListener('pointerdown', outside); document.addEventListener('keydown', key, true); });
onBeforeUnmount(() => { document.removeEventListener('pointerdown', outside); document.removeEventListener('keydown', key, true); });
</script>

<template>
  <div ref="root" class="theme-control" @focusout="focusOut">
    <button ref="trigger" type="button" class="outline theme-trigger" :aria-label="`选择显示主题，当前${label}`" :aria-controls="menuId" :aria-expanded="open" @click="toggle">
      <svg v-if="effectiveTheme === 'light'" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/></svg>
      <svg v-else viewBox="0 0 24 24" aria-hidden="true"><path d="M20.5 14.5A9 9 0 0 1 9.5 3.5 9 9 0 1 0 20.5 14.5Z"/></svg>
      <span>{{ label }}</span><small aria-hidden="true">⌄</small>
    </button>
    <fieldset v-if="open" :id="menuId" class="theme-options">
      <legend>显示主题</legend>
      <label v-for="option in options" :key="option.id" :class="{ chosen: themeMode === option.id }">
        <input type="radio" :name="`display-theme-${menuId}`" :value="option.id" :checked="themeMode === option.id" @change="setThemeMode(option.id)" @click="radioClick($event, option.id)">
        <span>{{ option.label }}</span><small v-if="option.id === 'system'">当前为{{ effectiveTheme === 'dark' ? '黑暗' : '明亮' }}</small>
      </label>
    </fieldset>
  </div>
</template>
