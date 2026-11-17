import test from 'node:test';
import assert from 'node:assert/strict';
import {createEventIdentityChecker,eventIdentityPayload,eventDisplayTitle} from '../src/lib/eventIdentity.js';

const form={host_contact_id:12,kinship_type_id:2,event_type_id:3,remark:' 新婚 ',event_date:'2026-10-06'};
test('事件身份只含事主亲属类型备注；日期改变不改变请求，排除正在修改的 ID',()=>{
  assert.deepEqual(eventIdentityPayload(form),{host_contact_id:12,kinship_type_id:2,event_type_id:3,remark:'新婚'});
  assert.deepEqual(eventIdentityPayload({...form,event_date:'2020-01-01'}),eventIdentityPayload(form));
  assert.deepEqual(eventIdentityPayload(form,17),{...eventIdentityPayload(form),exclude_event_id:17});
  for(const remark of [null,'','  '])assert.equal(eventIdentityPayload({...form,remark}).remark,null);
  assert.equal(eventIdentityPayload({...form,host_contact_id:null}),null);
  assert.equal(eventIdentityPayload({...form,remark:' Aa  Bb '}).remark,'Aa  Bb');
});
test('快速修改时慢旧请求不能覆盖当前事件查重',async()=>{
  const pending=[];const checker=createEventIdentityChecker(body=>new Promise(resolve=>pending.push({body,resolve})));
  const first=checker.check(eventIdentityPayload(form));
  const second=checker.check(eventIdentityPayload({...form,remark:'二婚'}));
  pending[1].resolve({existing_event:{event:{id:2}}});assert.deepEqual(await second,{current:true,existingEvent:{event:{id:2}}});
  pending[0].resolve({existing_event:{event:{id:1}}});assert.equal((await first).current,false);
});
test('关闭或登录边界作废请求，新会话不接受旧结果或旧错误',async()=>{
  let resolve,reject;const checker=createEventIdentityChecker(()=>new Promise((yes,no)=>{resolve=yes;reject=no;}));
  const old=checker.check(eventIdentityPayload(form));checker.invalidate();resolve({existing_event:{event:{id:1}}});assert.equal((await old).current,false);
  const bad=checker.check(eventIdentityPayload(form));checker.invalidate();reject(new Error('旧账号故障'));assert.equal((await bad).current,false);
  const empty=await checker.check(null);assert.deepEqual(empty,{current:true,existingEvent:null});
});
test('事件及记账标题追加事件备注，保留原身份，空备注不产生额外分隔符',()=>{
  const event={host:{id:12,name:'张建东'},kinship_type:{name:'之长女'},event_type:{name:'升学'},remark:'河北大学'};
  const contacts=[{id:12,is_self:false,relationship_type_name:'同事',remark:'项目组'}];
  assert.equal(eventDisplayTitle(event,contacts),'张建东(同事·项目组)之长女 · 升学 · 河北大学');
  assert.equal(eventDisplayTitle({event},contacts),eventDisplayTitle(event,contacts));
  assert.equal(eventDisplayTitle({...event,remark:null},contacts),'张建东(同事·项目组)之长女 · 升学');
  assert.equal(eventDisplayTitle({...event,remark:'  '},contacts),'张建东(同事·项目组)之长女 · 升学');
  assert.ok(!eventDisplayTitle({event,remark:'这是记账备注'},contacts).includes('这是记账备注'));
  assert.equal(eventDisplayTitle({...event,host:{id:9000,name:'本人'}},[{id:9000,is_self:true}]),'本人之长女 · 升学 · 河北大学');
});
