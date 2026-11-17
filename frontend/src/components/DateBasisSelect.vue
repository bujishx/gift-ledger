<script>
import { defineComponent, h, inject, nextTick, onMounted, onUnmounted, ref, Teleport, useId } from 'vue';
import { filterDialogContext } from './filterDialogContext.js';
export default defineComponent({
  props: { modelValue: String }, emits: ['update:modelValue'],
  setup(props, { emit }) {
    const modal = inject(filterDialogContext), root = ref(null), trigger = ref(null), popup = ref(null), open = ref(false), placement = ref({});
    const labelId = useId(), menuId = useId();
    const options = [{ value: 'event', label: '办事日期（收支归属）' }, { value: 'registered', label: '登记日期（录入情况）' }];
    const controller = { close(focus = false) { open.value = false; modal?.deactivate(controller); if (focus === true) trigger.value?.focus({ preventScroll: true }); } };
    function position() {
      if (!open.value || !popup.value || !trigger.value || !modal?.body.value || !modal?.popupHost.value) return;
      const anchor = trigger.value.getBoundingClientRect(), bounds = modal.body.value.getBoundingClientRect(), host = modal.popupHost.value.getBoundingClientRect();
      if (anchor.bottom <= bounds.top || anchor.top >= bounds.bottom) { controller.close(); return; }
      const desired = Math.min(popup.value.scrollHeight + 2, 160), below = Math.max(0, bounds.bottom - anchor.bottom - 5), above = Math.max(0, anchor.top - bounds.top - 5);
      const down = below >= desired || below >= above, available = Math.max(1, Math.min(desired, down ? below : above));
      placement.value = { left: `${anchor.left - host.left}px`, top: `${(down ? anchor.bottom + 3 : anchor.top - available - 3) - host.top}px`, width: `${anchor.width}px`, maxHeight: `${available}px` };
    }
    async function show() {
      if (open.value) { controller.close(true); return; }
      modal?.activate(controller); open.value = true; await nextTick(); position();
    }
    function outside(event) { if (!root.value?.contains(event.target) && !popup.value?.contains(event.target)) controller.close(); }
    function focusOutside(event) { if (open.value && !root.value?.contains(event.target) && !popup.value?.contains(event.target)) controller.close(); }
    function key(event) {
      if (!open.value) return;
      if (!root.value?.contains(event.target) && !popup.value?.contains(event.target)) return;
      if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); controller.close(true); }
      if (['ArrowDown', 'ArrowUp'].includes(event.key)) {
        event.preventDefault(); const controls = [...popup.value.querySelectorAll('button')], index = controls.indexOf(document.activeElement);
        controls[(index + (event.key === 'ArrowDown' ? 1 : -1) + controls.length) % controls.length]?.focus();
      }
    }
    onMounted(() => {
      document.addEventListener('pointerdown', outside, true); document.addEventListener('keydown', key, true);
      document.addEventListener('focusin', focusOutside);
      modal?.body.value?.addEventListener('scroll', position, { passive: true }); window.addEventListener('resize', position);
    });
    onUnmounted(() => {
      controller.close(); document.removeEventListener('pointerdown', outside, true); document.removeEventListener('keydown', key, true);
      document.removeEventListener('focusin', focusOutside);
      modal?.body.value?.removeEventListener('scroll', position); window.removeEventListener('resize', position);
    });
    return () => h('div', { ref: root, class: 'field multi-select dialog-multi-select statistics-single-select' }, [
      h('span', { id: labelId, class: 'field-label' }, '日期口径'),
      h('button', { ref: trigger, type: 'button', class: 'select-trigger', 'aria-expanded': open.value, 'aria-haspopup': 'listbox', 'aria-labelledby': labelId, 'aria-controls': menuId, onClick: show, onKeydown: async event => {
        if (!open.value && ['ArrowDown', 'ArrowUp'].includes(event.key)) { event.preventDefault(); await show(); popup.value?.querySelector('button')?.focus(); }
      } }, [h('span', { class: 'selection-text' }, options.find(option => option.value === props.modelValue)?.label), h('span', { class: 'selection-caret', 'aria-hidden': 'true' }, open.value ? '▴' : '▾')]),
      h(Teleport, { to: modal?.popupHost.value || 'body', disabled: !modal?.popupHost.value }, open.value ? [h('div', { ref: popup, id: menuId, class: 'option-menu filter-option-menu statistics-date-menu', style: placement.value, role: 'listbox', 'aria-labelledby': labelId }, options.map(option => h('button', {
        type: 'button', class: ['option-row', { selected: props.modelValue === option.value }], role: 'option', 'aria-selected': props.modelValue === option.value,
        onClick: () => { emit('update:modelValue', option.value); controller.close(true); },
      }, [h('span', { class: ['statistics-radio', { selected: props.modelValue === option.value }], 'aria-hidden': 'true' }), h('span', option.label)])))] : []),
    ]);
  },
});


</script>
