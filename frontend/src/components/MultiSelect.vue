<script setup>
import { computed, inject, nextTick, onMounted, onUnmounted, ref, useId, watch } from "vue";
import { filterDialogContext } from './filterDialogContext.js';

const props = defineProps({
  label: { type: String, required: true },
  options: { type: Array, default: () => [] },
  modelValue: { type: Array, default: () => [] },
  allLabel: { type: String, default: "全部" },
});
const emit = defineEmits(["update:modelValue"]);
const modal = inject(filterDialogContext, null);
const open = ref(false);
const root = ref(null);
const trigger = ref(null), popup = ref(null), placement = ref({});
const labelId = useId(), menuId = useId();
let frame = 0;
const selectedLabel = computed(() => {
  if (!props.modelValue.length) return props.allLabel;
  const labels = props.options
    .filter((x) => props.modelValue.includes(x.value))
    .map((x) => x.label);
  return labels.join("、") || props.allLabel;
});
function toggle(value) {
  const next = props.modelValue.includes(value)
    ? props.modelValue.filter((x) => x !== value)
    : [...props.modelValue, value];
  emit("update:modelValue", next);
}
function outside(event) {
  if (!root.value?.contains(event.target) && !popup.value?.contains(event.target)) controller.close();
}
const controller = { close(focus = false) { open.value = false; modal?.deactivate(controller); if (focus === true) trigger.value?.focus({ preventScroll: true }); } };
async function show() {
  if (open.value) { controller.close(true); return; }
  modal?.activate(controller); open.value = true;
  await nextTick();
  if (popup.value) popup.value.scrollTop = 0;
  position();
}
function position() {
  if (!modal || !open.value || !popup.value || !trigger.value || !modal.body.value || !modal.popupHost.value) return;
  const anchor = trigger.value.getBoundingClientRect(), bounds = modal.body.value.getBoundingClientRect(), host = modal.popupHost.value.getBoundingClientRect();
  if (anchor.bottom <= bounds.top || anchor.top >= bounds.bottom) { controller.close(); return; }
  popup.value.style.width = `${anchor.width}px`;
  // Do not temporarily expand max-height: that would clamp scrollTop after selecting the last item.
  const desired = Math.min(popup.value.scrollHeight + 2, 320);
  const below = Math.max(0, bounds.bottom - anchor.bottom - 5), above = Math.max(0, anchor.top - bounds.top - 5);
  const down = below >= desired || below >= above;
  const available = Math.max(1, Math.min(desired, down ? below : above));
  placement.value = { left: `${anchor.left - host.left}px`, width: `${anchor.width}px`, maxHeight: `${available}px`, top: `${(down ? anchor.bottom + 3 : anchor.top - available - 3) - host.top}px` };
  popup.value.dataset.direction = down ? 'down' : 'up';
}
function schedulePosition() { if (frame) cancelAnimationFrame(frame); frame = requestAnimationFrame(() => { frame = 0; position(); }); }
function focusOutside(event) { if (modal && open.value && !root.value?.contains(event.target) && !popup.value?.contains(event.target)) controller.close(); }
function clear() { emit('update:modelValue', []); controller.close(true); }
async function keyboard(event) {
  if (!open.value && (event.key === 'ArrowDown' || event.key === 'ArrowUp') && event.target === trigger.value) {
    event.preventDefault(); await show();
    const controls = [...popup.value.querySelectorAll('button,input')];
    (event.key === 'ArrowUp' ? controls.at(-1) : controls[0])?.focus(); return;
  }
  if (!open.value) return;
  if (event.key === "Escape" && open.value) {
    event.preventDefault(); event.stopImmediatePropagation(); controller.close(true); return;
  }
  if (!modal || event.key !== 'Tab') return;
  const controls = [...(popup.value?.querySelectorAll('button,input') || [])];
  if (event.target === trigger.value && !event.shiftKey) { event.preventDefault(); controls[0]?.focus(); }
  else if (event.target === controls[0] && event.shiftKey) { event.preventDefault(); controller.close(true); }
  else if (event.target === controls.at(-1) && !event.shiftKey) {
    event.preventDefault();
    const fields = [...modal.body.value.querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled)')].filter(node => node.getClientRects().length);
    const next = fields[fields.indexOf(trigger.value) + 1] || modal.panel.value.querySelector('.filter-dialog-footer button');
    controller.close(); next?.focus();
  }
}
watch(() => props.modelValue, () => nextTick(schedulePosition), { deep: true });
onMounted(() => {
  document.addEventListener('pointerdown', outside, true);
  document.addEventListener('keydown', keyboard, true);
  document.addEventListener('focusin', focusOutside);
  document.addEventListener('ledger-close-menus', controller.close);
  if (modal) {
    modal.body.value?.addEventListener('scroll', schedulePosition, { passive: true });
    window.addEventListener('resize', schedulePosition);
    window.visualViewport?.addEventListener('resize', schedulePosition);
  }
});
onUnmounted(() => {
  controller.close(); if (frame) cancelAnimationFrame(frame);
  document.removeEventListener('pointerdown', outside, true);
  document.removeEventListener('keydown', keyboard, true);
  document.removeEventListener('focusin', focusOutside);
  document.removeEventListener('ledger-close-menus', controller.close);
  modal?.body.value?.removeEventListener('scroll', schedulePosition);
  window.removeEventListener('resize', schedulePosition);
  window.visualViewport?.removeEventListener('resize', schedulePosition);
});
</script>

<template>
  <div ref="root" class="field multi-select" :class="{ 'dialog-multi-select': modal }">
    <span :id="labelId" class="field-label">{{ label }}</span>
    <button
      ref="trigger"
      type="button"
      class="select-trigger"
      :aria-expanded="open"
      :aria-labelledby="labelId"
      :aria-controls="menuId"
      :title="selectedLabel"
      @click="show"
    >
      <span class="selection-text">{{ selectedLabel }}</span>
      <small v-if="modal && modelValue.length" class="selection-total">已选 {{ modelValue.length }} 项</small>
      <span class="selection-caret" aria-hidden="true">{{ open ? '▴' : '▾' }}</span>
    </button>
    <Teleport :to="modal?.popupHost.value || 'body'" :disabled="!modal?.popupHost.value">
    <div v-if="open" :id="menuId" ref="popup" class="option-menu" :class="{ 'filter-option-menu': modal }" :style="modal ? placement : undefined" role="group" :aria-labelledby="labelId">
      <button
        type="button"
        class="option-clear"
        @click="clear"
      >
        {{ allLabel }}
      </button>
      <label v-for="option in options" :key="option.value" class="option-row" :class="{ selected: modelValue.includes(option.value) }">
        <input
          type="checkbox"
          :checked="modelValue.includes(option.value)"
          @change="toggle(option.value)"
        />
        <span>{{ option.label }}</span>
      </label>
    </div>
    </Teleport>
  </div>
</template>

<style scoped>
.dialog-multi-select .select-trigger { min-height: 54px; height: 54px; border-color: #d7e3f6; border-radius: 12px; gap: 9px; font-size: 14px; }
.dialog-multi-select .selection-text { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dialog-multi-select .selection-total { font-size: 11px; color: #61728b; flex-shrink: 0; font-weight: 400; }
.dialog-multi-select .selection-caret { color: #3d6aa5; }
.filter-option-menu { right: auto; bottom: auto; position: absolute; box-sizing: border-box; max-height: 320px; overflow-y: auto; overscroll-behavior: contain; scrollbar-gutter: stable; padding: 7px; pointer-events: auto; z-index: 1; }
.filter-option-menu .option-row { min-height: 40px; color: #455772; gap: 10px; }
.filter-option-menu .option-row span { overflow-wrap: anywhere; }
.filter-option-menu .option-row.selected { background: #edf4ff; }
.filter-option-menu .option-row input { flex-shrink: 0; width: 17px; height: 17px; padding: 0; }
.filter-option-menu .option-clear { min-height: 39px; font-size: 14px; }
</style>
