<script setup>
defineProps({ name: String });
const paths = {
  people: ['M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2','M16 3a4 4 0 0 1 0 8','M22 21v-2a4 4 0 0 0-3-3.87'],
  events: ['M8 2v4M16 2v4M3 10h18','M8 14h3M8 18h7'],
  ledger: ['M6 3h13a2 2 0 0 1 2 2v16H6a3 3 0 0 1 0-6h15', 'M6 3a3 3 0 0 0-3 3v12', 'm10 6 3 4 3-4M10 10h6M10 13h6M13 10v5'],
  stats: ['M4 21V10M10 21V3M16 21v-8M22 21H2'],
  me: ['M20 21v-2a7 7 0 0 0-14 0v2'],
  search: ['m21 21-4.35-4.35'],
};
</script>
<template>
  <svg class="ui-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <rect v-if="name === 'events'" x="3" y="4" width="18" height="18" rx="2" />
    <circle v-if="name === 'people'" cx="9" cy="7" r="4" />
    <circle v-if="name === 'me'" cx="13" cy="7" r="4" />
    <circle v-if="name === 'search'" cx="10.5" cy="10.5" r="7.5" />
    <path v-for="(path, index) in paths[name] || []" :key="index" :d="path" />
  </svg>
</template>
