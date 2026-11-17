import { reactive } from 'vue';
export function useDialog() {
  const state = reactive({open:false,title:'',message:'',input:false,value:'',inputLabel:'名称',confirmLabel:'确认',cancelLabel:'取消',confirmTone:'normal',busy:false,error:''});
  let resolve, onConfirm;
  function ask(options) {
    if(resolve) resolve(false);
    Object.assign(state,{open:true,input:false,value:'',inputLabel:'名称',confirmLabel:'确认',cancelLabel:'取消',confirmTone:'normal',busy:false,error:''},options);
    onConfirm = options.onConfirm;
    return new Promise(done=>{resolve = done;});
  }
  async function answer(accepted) {
    if(state.busy) return;
    if(accepted && onConfirm) {
      state.busy = true; state.error = '';
      try { await onConfirm(state.value.trim()); }
      catch(err) { state.error = err.message || '操作失败，请重试'; state.busy = false; return; }
    }
    state.open = false; state.busy = false;
    const done = resolve; resolve = null;
    done?.(accepted ? (state.input ? state.value.trim() : true) : false);
  }
  return {state,ask,answer};
}
