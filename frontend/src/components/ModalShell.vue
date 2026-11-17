<script setup>
import { ref, nextTick, onMounted, onUnmounted, useId } from 'vue';
import { lockPageScroll, unlockPageScroll } from '../lib/modalScroll.js';
import { allowModalBackdropClose, modalFamilyLayout, phoneModalFamily } from '../lib/modalFamilyLayout.js';
defineOptions({ inheritAttrs: false });
const props = defineProps({ title: String, wide: Boolean, closingDisabled: Boolean, family: Boolean });
const emit = defineEmits(['close']);
const panel = ref(null), titleId = useId();
const familyLayout = modalFamilyLayout(() => panel.value, { kind: 'entry' });
let previousFocus;
function key(e) {
  if(!panel.value || !panel.value.contains(document.activeElement)) return;
  if(e.key !== 'Tab') return;
  const list = [...panel.value.querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),a[href],[tabindex="0"]')].filter(x=>x.getClientRects().length && !x.closest('[inert]'));
  if(!list.length) { e.preventDefault(); panel.value.focus(); return; }
  if(e.shiftKey && (document.activeElement === list[0] || document.activeElement === panel.value)) { e.preventDefault(); list.at(-1).focus(); }
  else if(!e.shiftKey && document.activeElement === list.at(-1)) { e.preventDefault(); list[0].focus(); }
}
onMounted(async()=> {
  previousFocus = document.activeElement;
  lockPageScroll();
  await nextTick();
  if (props.family) familyLayout.start();
  (props.family && phoneModalFamily() ? panel.value : (panel.value.querySelector('input:not(:disabled),select:not(:disabled),textarea:not(:disabled),button:not(:disabled)') || panel.value)).focus({preventScroll:true});
  document.addEventListener('keydown',key);
});
onUnmounted(()=> {
  document.removeEventListener('keydown',key); familyLayout.stop(); unlockPageScroll();
  nextTick(()=> {
    if(previousFocus?.isConnected && !previousFocus.disabled && !previousFocus.closest('[inert]')) previousFocus.focus({preventScroll:true});
    else document.querySelector('.modal-shell')?.focus({preventScroll:true});
  });
});
</script>
<template>
  <Teleport to="body">
    <div class="modal-backdrop" :class="[$attrs.class, {'family-backdrop':family}]" @click.self="!closingDisabled && (!family || allowModalBackdropClose($event)) && emit('close')">
      <section ref="panel" class="modal modal-shell card" :class="{'entry-modal':wide, 'family-modal':family}" tabindex="-1" role="dialog" aria-modal="true" :aria-labelledby="titleId">
        <header class="modal-head"><h2 :id="titleId">{{title}}</h2><button class="icon-button" :disabled="closingDisabled" :aria-label="'关闭'+title" @click="emit('close')"><span v-if="family" class="family-close-label">关闭</span><span :aria-hidden="family ? true : undefined">×</span></button></header>
        <div class="modal-body"><slot /></div>
        <footer v-if="$slots.footer" class="modal-footer"><slot name="footer" /></footer>
      </section>
    </div>
  </Teleport>
</template>
