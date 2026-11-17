<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';
import './listWorkspace.css';

const props = defineProps({ title: String, page: Number, pageSize: Number, total: Number, unit: String, loading: Boolean, blocked: Boolean });
const emit = defineEmits(['update:page', 'update:pageSize']);
const root = ref(null), controls = ref(null), mirror = ref(null), rail = ref(null);
const mirrorVisible = ref(false), railVisible = ref(false), railStyle = ref({}), trackWidth = ref(0);
const pages = computed(() => Math.max(1, Math.ceil((props.total || 0) / (props.pageSize || 20))));
const tokens = computed(() => {
  const values = [...new Set([1, props.page - 1, props.page, props.page + 1, pages.value])].filter(x => x > 0 && x <= pages.value).sort((a, b) => a - b), result = [];
  values.forEach((value, index) => { if (index && value - values[index - 1] > 1) result.push('gap-' + value); result.push(value); });
  return result;
});
let scroller, table, observer, resizeObserver, frame, disposed = false, synchronizing = false, headerSignature = '', pendingMirrorColumn = null;
function schedule() { if (!frame && !disposed) frame = requestAnimationFrame(measure); }
function connect() {
  const current = root.value?.querySelector('.table-scroller');
  if (current === scroller) return;
  scroller?.removeEventListener('scroll', syncFromTable);
  scroller = current; table = scroller?.querySelector('table');
  scroller?.addEventListener('scroll', syncFromTable, { passive: true });
  if (scroller) resizeObserver?.observe(scroller);
  if (table) resizeObserver?.observe(table);
}
function syncFromTable() {
  if (!scroller) return;
  synchronizing = true;
  if (mirror.value) mirror.value.scrollLeft = scroller.scrollLeft;
  if (rail.value && Math.abs(rail.value.scrollLeft - scroller.scrollLeft) > 1) rail.value.scrollLeft = scroller.scrollLeft;
  synchronizing = false;
}
function syncFromRail() {
  if (!synchronizing && scroller && rail.value && Math.abs(scroller.scrollLeft - rail.value.scrollLeft) > 1) scroller.scrollLeft = rail.value.scrollLeft;
  if (mirror.value && scroller) mirror.value.scrollLeft = scroller.scrollLeft;
}
function copyHeader() {
  const head = table?.tHead;
  if (!head || !mirror.value) return;
  const widths = [...head.rows[0].cells].map(cell => cell.getBoundingClientRect().width);
  const width = table.getBoundingClientRect().width;
  const signature = head.innerHTML + widths.map(x => x.toFixed(1)).join(',') + width.toFixed(1);
  if (signature === headerSignature) return;
  const active = document.activeElement?.closest('[data-mirror-column]');
  const focusMayReturn = document.activeElement === document.body || mirror.value.contains(document.activeElement);
  const focused = active && mirror.value.contains(active) ? Number(active.dataset.mirrorColumn) : focusMayReturn ? pendingMirrorColumn : null;
  const clone = document.createElement('table'); clone.className = table.className;
  clone.style.width = width + 'px'; clone.style.minWidth = width + 'px'; clone.style.tableLayout = 'fixed';
  clone.setAttribute('aria-label', props.title + '固定表头');
  const colgroup = document.createElement('colgroup');
  widths.forEach(value => { const col = document.createElement('col'); col.style.width = value + 'px'; colgroup.append(col); });
  clone.append(colgroup);
  const clonedHead = head.cloneNode(true);
  [...clonedHead.rows[0].cells].forEach((cell, index) => {
    cell.style.width = widths[index] + 'px'; cell.style.minWidth = widths[index] + 'px'; cell.style.maxWidth = widths[index] + 'px';
    const button = cell.querySelector('button'); if (button) { button.dataset.mirrorColumn = index; button.disabled = props.loading; }
    cell.querySelectorAll('[id]').forEach(node => node.removeAttribute('id'));
  });
  clone.append(clonedHead); mirror.value.replaceChildren(clone); headerSignature = signature;
  if (focused !== null && mirrorVisible.value) mirror.value.querySelector(`[data-mirror-column="${focused}"]`)?.focus({ preventScroll: true });
  mirror.value.scrollLeft = scroller.scrollLeft;
}
function measure() {
  frame = null; connect();
  if (!root.value || !controls.value) return;
  const panel = controls.value.getBoundingClientRect();
  root.value.style.setProperty('--workspace-control-height', panel.height + 'px');
  const visibleTable = scroller && scroller.getClientRects().length && window.innerWidth > 700;
  if (!visibleTable) { mirrorVisible.value = railVisible.value = false; return; }
  const rect = scroller.getBoundingClientRect(), header = table?.tHead?.getBoundingClientRect();
  copyHeader();
  const nextMirror = Boolean(!props.blocked && header && header.top < panel.bottom - 1 && rect.bottom > panel.bottom + header.height);
  if (mirrorVisible.value && !nextMirror && mirror.value?.contains(document.activeElement)) {
    const column = Number(document.activeElement.dataset.mirrorColumn);
    table?.tHead?.rows[0].cells[column]?.querySelector('button')?.focus({ preventScroll: true });
  }
  mirrorVisible.value = nextMirror;
  if (pendingMirrorColumn !== null && nextMirror && !props.loading) {
    const focusedColumn = pendingMirrorColumn;
    nextTick(() => {
      if (document.activeElement === document.body || mirror.value?.contains(document.activeElement)) mirror.value?.querySelector(`[data-mirror-column="${focusedColumn}"]`)?.focus({ preventScroll: true });
      if (pendingMirrorColumn === focusedColumn) pendingMirrorColumn = null;
    });
  }
  const left = Math.max(0, rect.left), right = Math.min(window.innerWidth, rect.right);
  const originalRailTop = rect.bottom - Math.max(2, scroller.offsetHeight - scroller.clientHeight);
  railVisible.value = Boolean(!props.blocked && scroller.scrollWidth > scroller.clientWidth + 1 && rect.top < window.innerHeight - 28 && originalRailTop >= window.innerHeight && right > left);
  trackWidth.value = scroller.scrollWidth;
  railStyle.value = { left: left + 'px', width: Math.max(0, right - left) + 'px' };
  nextTick(syncFromTable);
}
async function sortFromMirror(event) {
  const button = event.target.closest('[data-mirror-column]');
  if (!button || props.loading || props.blocked) return;
  const column = Number(button.dataset.mirrorColumn);
  pendingMirrorColumn = column;
  table?.tHead?.rows[0].cells[column]?.querySelector('button')?.click();
  await nextTick(); headerSignature = ''; measure();
  mirror.value?.querySelector(`[data-mirror-column="${column}"]`)?.focus({ preventScroll: true });
}
async function returnToStart() {
  await nextTick();
  if (root.value) window.scrollTo({ top: root.value.getBoundingClientRect().top + window.scrollY - 4, behavior: 'instant' });
}
function setPage(value) { emit('update:page', value); returnToStart(); }
function setSize(value) { emit('update:pageSize', value); returnToStart(); }
watch(() => [props.loading, props.blocked, props.total], () => { headerSignature = ''; schedule(); });
onMounted(() => {
  resizeObserver = new ResizeObserver(schedule); resizeObserver.observe(root.value); resizeObserver.observe(controls.value);
  observer = new MutationObserver(schedule); observer.observe(root.value.querySelector('.workspace-list'), { childList: true, subtree: true, characterData: true, attributes: true });
  window.addEventListener('scroll', schedule, { passive: true }); window.addEventListener('resize', schedule);
  window.visualViewport?.addEventListener('resize', schedule); schedule();
});
onUnmounted(() => {
  disposed = true; cancelAnimationFrame(frame); observer?.disconnect(); resizeObserver?.disconnect();
  scroller?.removeEventListener('scroll', syncFromTable); window.removeEventListener('scroll', schedule); window.removeEventListener('resize', schedule); window.visualViewport?.removeEventListener('resize', schedule);
});
</script>
<template>
  <div ref="root" class="list-workspace">
    <div ref="controls" class="workspace-controls">
      <div class="workspace-top">
      <h2 class="workspace-title">{{ title }}</h2>
      <div class="workspace-search"><slot name="search" /></div>
      <div class="workspace-add"><slot name="add" /></div>
      </div>
      <div class="workspace-bottom">
      <div class="workspace-meta"><slot name="sort" /><span class="workspace-count" role="status">{{ loading ? '正在加载…' : `共 ${total || 0} ${unit}` }}</span></div>
      <nav class="pagination workspace-pagination" :aria-label="title + '分页'">
        <label class="page-size">每页 <select :value="pageSize" @change="setSize(Number($event.target.value))"><option v-for="size in [20,50,100]" :key="size" :value="size">{{ size }} 条</option></select></label>
        <button :disabled="page <= 1 || loading" @click="setPage(page - 1)">上一页</button>
        <template v-for="token in tokens" :key="token"><span v-if="typeof token === 'string'" class="page-gap">…</span><strong v-else-if="token === page" aria-current="page">{{ token }}</strong><button v-else class="page-number" :disabled="loading" :aria-label="'第' + token + '页'" @click="setPage(token)">{{ token }}</button></template>
        <span class="workspace-page-count">第 {{ page }}／{{ pages }} 页</span><button :disabled="page >= pages || loading" @click="setPage(page + 1)">下一页</button>
      </nav>
      </div>
      <div ref="mirror" class="workspace-table-head" :class="{ visible: mirrorVisible }" :inert="!mirrorVisible || loading || blocked" :aria-hidden="!mirrorVisible" @click="sortFromMirror" />
    </div>
    <div class="workspace-list"><slot /></div>
    <Teleport to="body"><div v-show="railVisible" ref="rail" class="workspace-horizontal-rail" :style="railStyle" :inert="!railVisible || blocked" tabindex="0" role="region" :aria-label="title + '横向滚动条'" @scroll="syncFromRail"><div :style="{ width: trackWidth + 'px' }" /></div></Teleport>
  </div>
</template>
