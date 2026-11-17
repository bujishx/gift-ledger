import test from 'node:test';
import assert from 'node:assert/strict';
import { contactsForList, createDetailAvailability, detailCountPath } from '../src/lib/detailAvailability.js';

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const tick = () => Promise.resolve();

test('三种全量范围只带精确 ID，事件范围和全部往来范围分开且不继承列表条件', () => {
  assert.equal(detailCountPath('events', 12), '/events?host_contact_id=12&limit=1&offset=0');
  assert.equal(detailCountPath('gifts', 12), '/ledger/query/gifts?contact_id=12&limit=1&offset=0');
  assert.equal(detailCountPath('eventGifts', 57), '/ledger/query/gifts?event_id=57&limit=1&offset=0');
  for (const id of [0, null, -1, '12&keyword=别人', NaN, 1.5]) assert.throws(() => detailCountPath('events', id));
});

test('没有事主事件但有收入记录，事件入口无资料而全部往来入口仍可用', async () => {
  const availability = createDetailAvailability(path => Promise.resolve(path.startsWith('/events')
    ? { total: 0, items: [] }
    : { page: { total: 1 }, items: [{ amount: '500', direction: 'income' }] }));
  await Promise.all([availability.load('events', 12), availability.load('gifts', 12)]);
  assert.deepEqual(availability.read('events', 12), { status: 'ready', total: 0, error: null });
  assert.equal(availability.read('gifts', 12).total, 1);
});

test('零元纯实物及超过一页的记录按全量 total 判断，不依赖金额或返回项数量', async () => {
  const availability = createDetailAvailability(() => Promise.resolve({
    page: { total: 27 }, items: [{ amount: '0', gift_details: '一箱牛奶' }],
  }));
  await availability.load('gifts', 12);
  assert.equal(availability.read('gifts', 12).total, 27);
  assert.equal(availability.read('gifts', 12).status, 'ready');
});

test('快速切换联系人时，旧联系人的慢响应不会覆盖当前联系人的结果', async () => {
  const first = deferred(), second = deferred();
  const availability = createDetailAvailability(path => path.includes('=12&') ? first.promise : second.promise);
  const oldRequest = availability.load('events', 12), newRequest = availability.load('events', 13);
  second.resolve({ total: 0 }); await newRequest;
  first.resolve({ total: 4 }); await oldRequest;
  assert.equal(availability.read('events', 13).total, 0);
  assert.equal(availability.read('events', 12).total, 4);
});

test('查询失败保持未知而非零条，重试才能得到确定无资料状态', async () => {
  let fail = true;
  const availability = createDetailAvailability(() => fail ? Promise.reject(new Error('连接失败')) : Promise.resolve({ total: 0 }));
  const failure = await availability.load('events', 12);
  assert.equal(failure.status, 'error');
  assert.equal(failure.total, null);
  assert.equal(availability.read('events', 12).total, null);
  fail = false;
  assert.equal((await availability.load('events', 12, { refresh: true })).total, 0);
  assert.equal(availability.read('events', 12).status, 'ready');
});

test('无效响应也当检查失败，不推断为零条记录', async () => {
  for (const response of [{}, { total: null }, { total: -1 }, { total: '3' }]) {
    const availability = createDetailAvailability(() => Promise.resolve(response));
    const result = await availability.load('events', 12);
    assert.equal(result.status, 'error');
    assert.equal(result.total, null);
  }
});

test('刷新或账号切换作废旧请求，旧结果及旧错误不能覆盖新状态', async () => {
  for (const oldFailure of [false, true]) {
    const first = deferred(), second = deferred();
    let calls = 0;
    const availability = createDetailAvailability(() => (++calls === 1 ? first.promise : second.promise));
    const oldRequest = availability.load('events', 12); await tick();
    availability.invalidate();
    assert.equal(availability.read('events', 12).status, 'idle');
    const newRequest = availability.load('events', 12); await tick();
    second.resolve({ total: 2 }); await newRequest;
    if (oldFailure) first.reject(new Error('旧账号错误'));
    else first.resolve({ total: 0 });
    assert.equal((await oldRequest).current, false);
    assert.equal(availability.read('events', 12).total, 2);
  }
});

test('同时核对合并相同请求，点击时刷新绕过旧缓存并更新删除后的数量', async () => {
  const pending = deferred(); let calls = 0;
  const availability = createDetailAvailability(() => ++calls === 1 ? pending.promise : Promise.resolve({ total: 0 }));
  const first = availability.load('events', 12);
  const second = availability.load('events', 12, { refresh: true });
  assert.equal(first, second);
  pending.resolve({ total: 3 }); await first;
  await availability.load('events', 12); assert.equal(calls, 1);
  await availability.load('events', 12, { refresh: true });
  assert.equal(calls, 2);
  assert.equal(availability.read('events', 12).total, 0);
});

test('名单按 is_self 隐藏本人，原资料和本人 ID 保留给事主选择及自动收支', () => {
  const self = { id: 9001, is_self: true, name: '建东和李娜' };
  const contacts = [self, { id: 1, name: '我', is_self: false }, { id: 3, name: '王三', is_self: false }];
  const list = contactsForList(contacts);
  assert.deepEqual(list.map(contact => contact.id), [1, 3]);
  assert.equal(list.length, 2);
  assert.equal(contacts.length, 3);
  assert.equal(contacts.find(contact => contact.is_self), self);
  assert.equal(contactsForList([self]).length, 0);
});
