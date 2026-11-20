<script setup>
import { onMounted, onUnmounted, ref } from 'vue';
import ModalShell from './ModalShell.vue';
const emit = defineEmits(['close']);
const content = ref(null);
const aborter = new AbortController();
function closeWithEscape(event) {
  if (event.key !== 'Escape') return;
  event.preventDefault();
  event.stopPropagation();
  emit('close');
}
onMounted(async () => {
  document.addEventListener('keydown', closeWithEscape, true);
  try {
    const modulePath = '/maintenance/site-info.js';
    const { loadSiteAbout } = await import(/* @vite-ignore */ modulePath);
    if (!aborter.signal.aborted) await loadSiteAbout(content.value, {endpoint:'/api/v1/site/about', signal:aborter.signal});
  } catch {
    if (content.value) content.value.textContent = '暂时无法读取本站信息，请重新打开。';
  }
});
onUnmounted(() => {
  aborter.abort();
  document.removeEventListener('keydown', closeWithEscape, true);
});
</script>

<template>
  <ModalShell family class="compact-family-dialog" title="关于往来有礼" @close="emit('close')">
    <div ref="content" class="site-about-content" aria-live="polite">正在读取本站信息…</div>
    <template #footer>
      <div class="site-about-actions">
        <a class="cross-site-link" href="/maintenance" target="_blank" rel="noopener" aria-label="站点维护（在新标签打开）" title="站点维护（在新标签打开）">
          站点维护<svg aria-hidden="true"><use :href="'/maintenance/link-icons.svg#new-tab'"/></svg>
        </a>
        <button type="button" class="outline" @click="emit('close')">关闭</button>
      </div>
    </template>
  </ModalShell>
</template>
