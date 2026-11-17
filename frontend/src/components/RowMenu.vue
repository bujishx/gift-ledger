<script setup>
import { ref, nextTick, onMounted, onUnmounted } from 'vue';
const props = defineProps({ label: String, items: Array });
const emit = defineEmits(['select']);
const open = ref(false), trigger = ref(null), popup = ref(null), position = ref({left:0,top:0});
function close() { open.value = false; }
async function toggle() {
  const wasOpen = open.value;
  document.dispatchEvent(new Event('ledger-close-menus'));
  if(wasOpen) return;
  document.dispatchEvent(new Event('ledger-close-details'));
  const rect = trigger.value.getBoundingClientRect();
  position.value = { left: Math.max(8, Math.min(rect.right - 180, window.innerWidth - 188)), top: rect.bottom + 4 };
  open.value = true;
  await nextTick();
  if(popup.value) position.value.top = Math.max(8, Math.min(position.value.top, window.innerHeight - popup.value.offsetHeight - 8));
  popup.value?.querySelector('button:not(:disabled)')?.focus();
}
function select(item) { close(); trigger.value?.focus(); emit('select', item.id); }
function outside(e) { if(!trigger.value?.contains(e.target) && !popup.value?.contains(e.target)) close(); }
function key(e) {
  if(!open.value) return;
  if(e.key === 'Escape') { e.preventDefault(); e.stopImmediatePropagation(); close(); trigger.value?.focus(); }
  if(e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault();
    const buttons = [...popup.value.querySelectorAll('button:not(:disabled)')];
    const index = buttons.indexOf(document.activeElement);
    buttons[(index + (e.key === 'ArrowDown' ? 1 : buttons.length - 1)) % buttons.length]?.focus();
  }
}
onMounted(() => { document.addEventListener('pointerdown', outside); document.addEventListener('ledger-close-menus', close); document.addEventListener('keydown', key, true); window.addEventListener('resize', close); window.addEventListener('scroll', close, true); });
onUnmounted(() => { document.removeEventListener('pointerdown', outside); document.removeEventListener('ledger-close-menus', close); document.removeEventListener('keydown', key, true); window.removeEventListener('resize', close); window.removeEventListener('scroll', close, true); });
</script>
<template>
  <span class="row-menu">
    <button ref="trigger" type="button" class="icon-button row-menu-button" :aria-label="label" aria-haspopup="menu" :aria-expanded="open" @click.stop="toggle" @keydown.enter.stop @keydown.space.stop>⋯</button>
    <Teleport to="body"><div v-if="open" ref="popup" class="row-menu-pop floating-menu" role="menu" :aria-label="label" :style="{left:position.left+'px',top:position.top+'px'}" @click.stop>
      <button v-for="item in items" :key="item.id" role="menuitem" type="button" :class="{danger:item.danger}" :disabled="item.disabled" @click="select(item)">{{item.label}}</button>
    </div></Teleport>
  </span>
</template>
