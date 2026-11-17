<script setup>
import { computed } from 'vue';
const props = defineProps({ page: Number, pageSize: Number, total: Number, title: String, unit: String, loading: Boolean });
const emit = defineEmits(['update:page', 'update:pageSize']);
const pages = computed(() => Math.max(1, Math.ceil((props.total || 0) / props.pageSize)));
const tokens = computed(() => {
  const visible = [...new Set([1, props.page - 1, props.page, props.page + 1, pages.value])].filter(x => x > 0 && x <= pages.value).sort((a,b) => a-b);
  const result = [];
  visible.forEach((x,i) => { if(i && x - visible[i-1] > 1) result.push('gap-' + x); result.push(x); });
  return result;
});
</script>
<template>
  <div class="list-title">
    <h2>{{ title }}</h2>
    <slot />
    <nav class="pagination" :aria-label="title + '分页'">
      <label class="page-size">每页 <select :value="pageSize" @change="emit('update:pageSize', Number($event.target.value))"><option v-for="size in [20,50,100]" :key="size" :value="size">{{size}} 条</option></select></label>
      <button :disabled="page <= 1 || loading" @click="emit('update:page', page - 1)">上一页</button>
      <template v-for="token in tokens" :key="token">
        <span v-if="typeof token === 'string'" class="page-gap" aria-label="省略页码">…</span>
        <strong v-else-if="token === page" aria-current="page">{{token}}</strong>
        <button v-else class="page-number" :disabled="loading" :aria-label="'第' + token + '页'" @click="emit('update:page', token)">{{token}}</button>
      </template>
      <span class="mobile-page-count">第 {{page}}／{{pages}} 页</span>
      <button :disabled="page >= pages || loading" @click="emit('update:page', page + 1)">下一页</button>
    </nav>
    <span class="list-count" role="status">{{ loading ? '正在加载…' : `共 ${total || 0} ${unit}` }}</span>
  </div>
</template>
