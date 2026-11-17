<script setup>
import { onMounted, onUnmounted } from 'vue';
import ModalShell from './ModalShell.vue';
import { SITE_VERSION } from '../lib/siteVersion.js';
const emit = defineEmits(['close']);
function closeWithEscape(event) {
  if (event.key !== 'Escape') return;
  event.preventDefault();
  event.stopPropagation();
  emit('close');
}
onMounted(() => document.addEventListener('keydown', closeWithEscape, true));
onUnmounted(() => document.removeEventListener('keydown', closeWithEscape, true));
</script>

<template>
  <ModalShell family class="compact-family-dialog" title="关于往来有礼" @close="emit('close')">
    <div class="version-about-content">
      <img src="/brand-mark.png" alt="" width="52" height="52" />
      <h2>往来有礼</h2>
      <p class="version-slogan">记人情往来，守礼数分寸</p>
      <dl>
        <div><dt>网站版本</dt><dd>v{{ SITE_VERSION.version }}</dd></div>
        <div><dt>更新日期</dt><dd>{{ SITE_VERSION.updatedAt }}</dd></div>
      </dl>
    </div>
    <template #footer><button type="button" class="outline" @click="emit('close')">关闭</button></template>
  </ModalShell>
</template>

<style scoped>
.version-about-content { text-align:center; padding:8px 0; }
.version-about-content h2 { margin:12px 0 8px; }
.version-slogan { color:#64748b; font-size:14px; margin:0 0 24px; }
dl { margin:0; text-align:left; border-top:1px solid #e2e8f0; }
dl div { display:flex; justify-content:space-between; gap:12px; padding:15px 6px; border-bottom:1px solid #e2e8f0; }
dt { color:#64748b; }
dd { margin:0; color:#20374f; font-weight:600; }
</style>
