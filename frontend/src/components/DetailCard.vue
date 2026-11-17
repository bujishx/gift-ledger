<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { lockPageScroll, unlockPageScroll } from '../lib/modalScroll.js';
import { modalFamilyLayout } from '../lib/modalFamilyLayout.js';

const props = defineProps({
  title: { type: String, required: true },
  closeLabel: { type: String, required: true },
  position: { type: Object, required: true },
  setCardRef: { type: Function, required: true },
  phone: Boolean,
  cardClass: String,
});
const emit = defineEmits(['close', 'drag-start', 'drag-move', 'drag-end']);
const card = ref(null);
const familyLayout = modalFamilyLayout(() => card.value, { kind: 'detail', getPosition: () => props.position });
const layoutStyle = computed(() => ({
  '--detail-nav-height': `${props.position.navHeight || 0}px`,
  '--detail-card-gap': `${props.position.cardGap || 0}px`,
}));
function setRef(element) { card.value = element; props.setCardRef(element); }
function startDrag(event) {
  // 标题可以选择文字，电脑保留在标题旁空白处拖动的操作。
  if (event.target.closest('.detail-card-title')) return;
  emit('drag-start', event);
}
function trapFocus(event) {
  if (event.key !== 'Tab' || props.phone || !card.value || document.querySelector('.modal-backdrop')) return;
  const elements = [...card.value.querySelectorAll('button:not(:disabled),a[href],input:not(:disabled),select:not(:disabled),textarea:not(:disabled),[tabindex="0"]')]
    .filter(element => element.getClientRects().length && !element.closest('[inert]'));
  if (!elements.length) { event.preventDefault(); card.value.focus({preventScroll:true}); return; }
  const index = elements.indexOf(document.activeElement);
  if (event.shiftKey && index <= 0) { event.preventDefault(); elements.at(-1).focus(); }
  else if (!event.shiftKey && (index < 0 || index === elements.length-1)) { event.preventDefault(); elements[0].focus(); }
}
watch(() => [props.position.navHeight, props.position.cardGap], () => familyLayout.schedule());
onMounted(() => { lockPageScroll(); familyLayout.start(); document.addEventListener('keydown', trapFocus); });
onUnmounted(() => { document.removeEventListener('keydown', trapFocus); familyLayout.stop(); unlockPageScroll(); });
</script>

<template>
  <div class="drawer-backdrop family-backdrop" :style="layoutStyle" @click.self="emit('close')">
    <aside :ref="setRef" :class="['person-drawer','float-card','family-modal',cardClass]" role="dialog" tabindex="-1"
      :aria-label="title" :aria-modal="phone ? undefined : true"
      :style="{left:`${position.left}px`,top:`${position.top}px`}">
      <header class="detail-card-head person-drag-handle"
        @pointerdown="startDrag" @pointermove="emit('drag-move',$event)"
        @pointerup="emit('drag-end',$event)" @pointercancel="emit('drag-end',$event)">
        <h2 class="detail-card-title">{{title}}</h2>
        <button type="button" class="icon-button" :aria-label="closeLabel" @click="emit('close')">关闭 <span aria-hidden="true">×</span></button>
      </header>
      <div class="detail-card-body"><slot /></div>
    </aside>
  </div>
</template>
