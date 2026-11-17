import assert from 'node:assert/strict';
import test from 'node:test';
import {buildFilterChips,removeFilterChip} from '../src/lib/filterChips.js';
import {statisticsViewState,statisticsStorageKey,readStatisticsView,writeStatisticsView,clearStatisticsViews} from '../src/lib/statisticsSession.js';

test('删除多选标签只取消该值，保留同组其他选项、关键词与日期',()=>{
 const filters={relations:[2,3],keyword:'同名',date_from:'2026-01-01',date_to:'2026-09-30'};
 const chips=buildFilterChips(filters,[{key:'relations',label:'关系',options:[{value:2,label:'同事'},{value:3,label:'朋友'}]}],'办事日期');
 assert.deepEqual(chips.map(c=>c.label),['关系：同事','关系：朋友','办事日期：2026-01-01 至 2026-09-30']);
 removeFilterChip(filters,chips[0]);
 assert.deepEqual(filters,{relations:[3],keyword:'同名',date_from:'2026-01-01',date_to:'2026-09-30'});
 removeFilterChip(filters,chips[2]);
 assert.deepEqual(filters,{relations:[3],keyword:'同名',date_from:'',date_to:''});
});

function memoryStorage(){const values=new Map();return {get length(){return values.size;},key:index=>[...values.keys()][index],getItem:key=>values.get(key)||null,setItem:(key,value)=>values.set(key,value),removeItem:key=>values.delete(key)};}
test('同账号同登录会话恢复筛选，新会话和其他账号不继承旧条件',async()=>{
 const storage=memoryStorage(),a=await statisticsStorageKey(1,'session-a'),b=await statisticsStorageKey(1,'session-b'),other=await statisticsStorageKey(2,'session-a');
 assert.notEqual(a,b);assert.notEqual(a,other);assert.ok(!a.includes('session-a'));
 writeStatisticsView(a,{preset:'lastYear',counterparty_relationship_type_ids:[2,3],date_basis:'registered'},storage);
 assert.equal(readStatisticsView(a,storage).preset,'lastYear');
 assert.deepEqual(readStatisticsView(a,storage).counterparty_relationship_type_ids,[2,3]);
 assert.equal(readStatisticsView(b,storage).preset,'all');
 assert.deepEqual(readStatisticsView(other,storage).counterparty_relationship_type_ids,[]);
 storage.setItem('unrelated','keep');clearStatisticsViews(storage);
 assert.equal(readStatisticsView(a,storage).preset,'all');assert.equal(storage.getItem('unrelated'),'keep');
});
test('异常缓存和无存储权限时使用默认筛选，不中断页面',()=>{
 const storage=memoryStorage();storage.setItem('invalid','{broken');
 assert.equal(readStatisticsView('invalid',storage).preset,'all');
 assert.deepEqual(statisticsViewState({preset:'bad',contact_id:-1,date_basis:'bad'}),statisticsViewState());
 const denied={getItem(){throw new Error('denied');},setItem(){throw new Error('denied');}};
 assert.equal(readStatisticsView('key',denied).preset,'all');assert.doesNotThrow(()=>writeStatisticsView('key',{},denied));
});

test('旧统计缓存清除联系人与具体事件隐藏条件，其他共同条件迁移为多选',()=>{
 const filters={preset:'custom',year:2025,month:'2025-10',date_from:'2025-01-01',date_to:'2025-12-31',relationship_type_id:2,contact_id:12,event_type_id:3,event_id:9,interaction_status:'mutual',date_basis:'registered',more:true};
 for(const tab of ['people','reciprocity']){
  const storage=memoryStorage();
  storage.setItem('old',JSON.stringify({...filters,tab,sort:'net_amount',sortDirection:'asc'}));
  const restored=readStatisticsView('old',storage);
  assert.deepEqual(restored,{preset:'custom',year:2025,month:'2025-10',date_from:'2025-01-01',date_to:'2025-12-31',keyword:'',counterparty_relationship_type_ids:[2],event_type_ids:[3],interaction_statuses:['mutual'],date_basis:'registered',tab:'overview',grain:'auto'});
  for(const key of ['contact_id','event_id','more'])assert.ok(!(key in restored));
  writeStatisticsView('new',restored,storage);
  assert.deepEqual(JSON.parse(storage.getItem('new')),restored);
 }
});
test('三个有效统计分类继续保存在本登录会话，已删除排序不再存储',()=>{
 const storage=memoryStorage();
 for(const tab of ['overview','events','relationships']){
  writeStatisticsView(tab,{tab,preset:'lastYear',sort:'expense_amount',sortDirection:'asc'},storage);
  const saved=readStatisticsView(tab,storage);
  assert.equal(saved.tab,tab);assert.equal(saved.preset,'lastYear');
  assert.ok(!('sort' in saved));assert.ok(!('sortDirection' in saved));
 }
});
