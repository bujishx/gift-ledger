<script setup>
import { computed, nextTick, onMounted, onUnmounted, provide, ref, useId, watch } from 'vue';
import FilterChips from './FilterChips.vue';
import { filterDialogContext } from './filterDialogContext.js';
import { lockPageScroll, unlockPageScroll } from '../lib/modalScroll.js';
import { allowModalBackdropClose, modalFamilyLayout } from '../lib/modalFamilyLayout.js';

const props = defineProps({
  open: Boolean,
  filters: { type: Array, default: () => [] },
  resultCount: { type: Number, default: null },
  resultUnit: { type: String, default: '条' },
  loading: Boolean,
  error: { type: String, default: '' },
  validationError: { type: String, default: '' },
});
const emit = defineEmits(['close', 'remove-filter', 'clear-filters']);
const panel = ref(null), body = ref(null), popupHost = ref(null), titleId = useId();
const familyLayout = modalFamilyLayout(() => panel.value, { kind: 'filter' });
let previousFocus, locked = false, activeMenu = null;
const context = {
  panel, body, popupHost,
  activate(menu) { if (activeMenu && activeMenu !== menu) activeMenu.close(); activeMenu = menu; },
  deactivate(menu) { if (activeMenu === menu) activeMenu = null; },
  closeMenu(focus = false) { if (!activeMenu) return false; activeMenu.close(focus); activeMenu = null; return true; },
};
provide(filterDialogContext, context);
const feedback = computed(() => {
  if (props.validationError) return props.validationError;
  if (props.loading) return '正在更新结果…';
  if (props.error) return '更新失败，请重试';
  if (Number.isFinite(props.resultCount) && props.resultCount >= 0) return `匹配结果 ${props.resultCount} ${props.resultUnit}`;
  return '结果待更新';
});
function close() { context.closeMenu(); emit('close'); }
async function syncOpen() {
  await nextTick();
  if (!panel.value) return;
  if (props.open && !panel.value.open) {
    previousFocus = document.activeElement;
    context.closeMenu();
    if (!locked) { lockPageScroll(); locked = true; }
    panel.value.showModal();
    familyLayout.start();
    body.value.scrollTop = 0;
    (body.value.querySelector('button,input,select') || panel.value).focus({ preventScroll: true });
  } else if (!props.open && panel.value.open) {
    context.closeMenu(); panel.value.close(); familyLayout.stop();
    if (locked) { unlockPageScroll(); locked = false; }
    await nextTick();
    if (previousFocus?.isConnected && !previousFocus.disabled && !previousFocus.closest('[inert]')) previousFocus.focus({ preventScroll: true });
  }
}
function key(event) {
  if (!props.open || !panel.value?.open) return;
  if (event.key === 'Escape') {
    event.preventDefault(); event.stopImmediatePropagation();
    if (!context.closeMenu(true)) close();
    return;
  }
  if (event.key !== 'Tab') return;
  const nodes = [...panel.value.querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled),a[href],[tabindex="0"]')].filter(node => node.getClientRects().length && !node.closest('[inert]'));
  if (!nodes.length) { event.preventDefault(); panel.value.focus(); return; }
  if (event.shiftKey && (document.activeElement === nodes[0] || document.activeElement === panel.value)) { event.preventDefault(); nodes.at(-1).focus(); }
  else if (!event.shiftKey && document.activeElement === nodes.at(-1)) { event.preventDefault(); nodes[0].focus(); }
}
function backdrop(event) {
  if (event.target !== panel.value) return;
  if (!allowModalBackdropClose(event)) return;
  const rect = panel.value.getBoundingClientRect();
  if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) close();
}
watch(() => props.open, syncOpen);
onMounted(() => { document.addEventListener('keydown', key, true); syncOpen(); });
onUnmounted(() => {
  document.removeEventListener('keydown', key, true); context.closeMenu(); familyLayout.stop();
  if (locked) { unlockPageScroll(); locked = false; }
  if (panel.value?.open) panel.value.close();
  if (previousFocus?.isConnected && !previousFocus.disabled) previousFocus.focus({ preventScroll: true });
});
</script>

<template>
  <Teleport to="body">
    <dialog ref="panel" class="filter-dialog family-modal" :aria-labelledby="titleId" tabindex="-1" @cancel.prevent="close" @click="backdrop">
      <header class="filter-dialog-head">
        <div><h2 :id="titleId">筛选条件</h2><small>勾选立即生效，关闭后保留条件</small></div>
        <button type="button" class="icon-button" aria-label="关闭筛选条件" @click="close"><span class="family-close-label">关闭</span><span aria-hidden="true">×</span></button>
      </header>
      <div ref="body" class="filter-dialog-body">
        <div class="filter-dialog-fields"><slot /></div>
        <section class="filter-dialog-selected" aria-label="已选条件">
          <div class="selection-heading"><strong>已选条件</strong><button type="button" class="text-button" :disabled="!filters.length" @click="context.closeMenu(); emit('clear-filters')">清除全部</button></div>
          <FilterChips :filters="filters" @remove="emit('remove-filter', $event)" />
          <p v-if="!filters.length" class="filter-none">尚未设置筛选条件</p>
        </section>
      </div>
      <footer class="filter-dialog-footer">
        <div class="filter-dialog-feedback" role="status" aria-live="polite">
          <span><strong>{{ filters.length ? `已选 ${filters.length} 项条件` : '未设置筛选条件' }}</strong> · {{ feedback }}</span>
          <small>实时更新，“完成”只关闭窗口</small>
        </div>
        <button type="button" class="primary" @click="close">完成</button>
      </footer>
      <div ref="popupHost" class="filter-popup-host" />
    </dialog>
  </Teleport>
</template>

<style scoped>
.filter-dialog { padding: 0; border: 1px solid #d4deed; border-radius: 22px; width: min(620px, calc(100vw - 48px)); height: auto; min-height: 0; max-height: 80dvh; max-width: none; margin: 0 auto; inset-block-start: 50%; inset-block-end: auto; transform: translateY(-50%); background: #fff; color: #1c2737; box-shadow: 0 22px 64px #15263b40; overflow: hidden; }
.filter-dialog[open] { display: flex; flex-direction: column; }
.filter-dialog::backdrop { background: #24344866; }
.filter-dialog-head { display: flex; align-items: center; justify-content: space-between; flex-shrink: 0; padding: 20px 24px; background: #f6f9ff; border-bottom: 1px solid #dfe8f5; position: relative; z-index: 3; }
.filter-dialog-head h2 { font-size: 20px; margin: 0 0 5px; }
.filter-dialog-head small { color: #61718a; font-size: 12px; }
.filter-dialog-body { flex: 0 1 auto; min-height: 0; padding: 24px; overflow: auto; overscroll-behavior: contain; scrollbar-gutter: stable; }
.filter-dialog-fields { display: grid; grid-template-columns: minmax(0, 1fr); gap: 22px; }
.filter-dialog-fields :deep(.field) { width: 100%; min-width: 0; }
.filter-dialog-fields :deep(.date-field) { grid-column: auto; }
.filter-dialog-fields :deep(.date-field > div) { display: flex; align-items: center; gap: 9px; }
.filter-dialog-fields :deep(.date-field input) { min-width: 0; flex: 1; min-height: 52px; padding: 10px; }
.filter-dialog-selected { margin-top: 34px; padding-top: 18px; border-top: 1px solid #e4eaf4; }
.selection-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; font-size: 14px; }
.filter-dialog-selected :deep(.filter-chips) { display: flex; flex-wrap: wrap; height: auto; max-height: none; overflow: visible; margin-top: 12px; gap: 8px; }
.filter-dialog-selected :deep(.filter-chip) { max-width: 100%; white-space: normal; }
.filter-dialog-selected :deep(.filter-chip span:first-child) { overflow-wrap: anywhere; }
.filter-none { color: #738099; font-size: 13px; margin-bottom: 0; }
.filter-dialog-footer { display: flex; align-items: center; justify-content: space-between; gap: 15px; flex-shrink: 0; padding: 16px 24px; border-top: 1px solid #dfe8f5; background: #f6f9ff; position: relative; z-index: 3; }
.filter-dialog-feedback { min-width: 0; font-size: 13px; color: #526681; }
.filter-dialog-feedback strong { color: #0b54c5; }
.filter-dialog-feedback small { display: block; font-size: 12px; margin-top: 5px; }
.filter-dialog-footer .primary { min-width: 80px; flex-shrink: 0; }
.filter-popup-host { position: absolute; inset: 0; pointer-events: none; z-index: 2; }
@media (max-width: 760px) { .filter-dialog-head { padding: 16px 18px; } .filter-dialog-body { padding: 22px 18px; } .filter-dialog-footer { padding: 14px 18px; gap: 10px; } .filter-dialog-feedback { font-size: 12px; } .filter-dialog-footer .primary { min-width: 70px; padding: 10px 18px; } }
@media (max-width: 500px) {
  .filter-dialog { width: calc(100vw - 32px); max-height: min(82dvh, calc(100dvh - 32px - env(safe-area-inset-bottom))); border-radius: 20px; transform: none; inset-block-start: auto; inset-block-end: calc(16px + env(safe-area-inset-bottom)); }
  .filter-dialog-head { padding: 14px 16px; }
  .filter-dialog-body { padding: 18px 16px; }
  .filter-dialog-fields { gap: 18px; }
  .filter-dialog-fields :deep(.dialog-multi-select .select-trigger) { height: 50px; min-height: 50px; }
  .filter-dialog-fields :deep(.date-field input) { min-height: 50px; }
  .filter-dialog-selected { margin-top: 24px; padding-top: 16px; }
  .filter-dialog-footer { padding: 12px 16px; }
  .filter-dialog-feedback { flex: 1; }
}
@media (max-width: 360px) {
  .filter-dialog-feedback > span { display: block; min-height: 2lh; }
}
</style>
