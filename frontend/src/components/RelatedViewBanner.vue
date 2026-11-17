<script setup>
defineProps({title:String,items:{type:Array,default:()=>[]},count:Number,unit:String,sourceLabel:String,loading:Boolean});
defineEmits(['exit','back','remove']);
</script>
<template>
 <aside class="related-view" aria-label="关联查看范围">
  <div class="related-heading"><span class="related-badge">↗ 关联查看</span><strong>{{title}}</strong></div>
  <p class="related-count" role="status">{{loading?'正在查询…':`当前条件下共 ${count || 0} ${unit || ''}`}}<span>常规筛选在此范围内继续筛选</span></p>
  <div v-if="items.length" class="related-conditions" aria-label="关联查看附加条件">
   <button v-for="item in items" :key="item.key" type="button" :aria-label="'取消关联条件：'+item.label" @click="$emit('remove',item.key)">{{item.label}} <span aria-hidden="true">×</span></button>
  </div>
  <div class="related-actions"><button type="button" class="outline" @click="$emit('back')">← 返回{{sourceLabel}}</button><button type="button" class="related-exit" @click="$emit('exit')">退出关联查看 ×</button></div>
  <small>退出后恢复原列表；通过主导航切换页面，也会结束本次关联查看。</small>
 </aside>
</template>
<style scoped>
.related-view{margin:0 0 16px;padding:17px 18px;background:#edf4ff;border:2px solid #2675de;border-left:6px solid #1258bb;border-radius:16px;color:#133d73;box-shadow:0 4px 14px #195dbb0d;overflow-wrap:anywhere}
.related-heading{display:flex;align-items:center;flex-wrap:wrap;gap:9px}.related-heading strong{font-size:17px;line-height:1.5}.related-badge{background:#174f9f;color:white;font-size:12px;font-weight:700;border-radius:6px;padding:4px 8px;white-space:nowrap}
.related-count{margin:9px 0;font-size:13px;line-height:1.6}.related-count span{display:block;color:#49627f}.related-actions{display:flex;gap:9px;flex-wrap:wrap;align-items:center;margin:12px 0 9px}.related-actions button{padding:9px 15px;min-height:44px;font-size:14px}.related-exit{border:1px solid #174f9f;background:#174f9f;color:white;border-radius:24px;font-weight:700}.related-view small{font-size:12px;line-height:1.6;display:block;color:#526887}.related-conditions{display:flex;gap:7px;flex-wrap:wrap}.related-conditions button{background:white;border:1px solid #adc9ed;border-radius:9px;padding:8px 10px;color:#173f74;font-size:13px;text-align:left;min-height:40px}.related-conditions button span{margin-left:8px;font-size:16px}
@media(max-width:700px){.related-view{padding:13px 13px;border-radius:13px}.related-heading strong{font-size:15px}.related-actions button{flex:1;white-space:nowrap;padding:9px 11px;font-size:13px}}
</style>
