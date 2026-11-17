const emptyState = () => ({ status: 'idle', total: null, error: null });

/** 全量计数不能继承当前列表的搜索、日期、筛选或分页。 */
export function detailCountPath(kind, contactId) {
  const id = Number(contactId);
  if (!Number.isSafeInteger(id) || id <= 0) throw new Error('联系人编号不可用');
  if (kind === 'events') return `/events?host_contact_id=${id}&limit=1&offset=0`;
  if (kind === 'gifts') return `/ledger/query/gifts?contact_id=${id}&limit=1&offset=0`;
  if (kind === 'eventGifts') return `/ledger/query/gifts?event_id=${id}&limit=1&offset=0`;
  throw new Error('未知的详情资料范围');
}

/** 只供名单展示使用；原 contacts 仍保留本人供建档和自动收支判断。 */
export function contactsForList(contacts) {
  return contacts.filter(contact => !contact.is_self);
}

/** 可传 Vue reactive 对象；请求失败保留未知状态，绝不当作零条记录。 */
export function createDetailAvailability(request, states = {}) {
  let generation = 0;
  const pending = new Map();
  const keyFor = (kind, id) => `${kind}:${Number(id)}`;

  function read(kind, id) {
    return states[keyFor(kind, id)] || emptyState();
  }
  function invalidate() {
    generation++;
    pending.clear();
    for (const key of Object.keys(states)) delete states[key];
  }
  function load(kind, id, { refresh = false } = {}) {
    let path;
    try { path = detailCountPath(kind, id); }
    catch (error) { return Promise.resolve({ current: false, ...emptyState(), error }); }
    const key = keyFor(kind, id);
    if (pending.has(key)) return pending.get(key);
    const state = read(kind, id);
    if (!refresh && state.status === 'ready') return Promise.resolve({ current: true, ...state });
    const startedGeneration = generation;
    states[key] = { status: 'loading', total: null, error: null };
    const task = Promise.resolve().then(() => request(path)).then(response => {
      const total = kind === 'events' ? response?.total : response?.page?.total;
      if (!Number.isSafeInteger(total) || total < 0) throw new Error('资料数量检查未返回有效结果');
      if (startedGeneration !== generation) return { current: false, ...emptyState() };
      states[key] = { status: 'ready', total, error: null };
      return { current: true, ...states[key] };
    }).catch(error => {
      if (startedGeneration !== generation) return { current: false, ...emptyState() };
      states[key] = { status: 'error', total: null, error };
      return { current: true, ...states[key] };
    }).finally(() => {
      if (pending.get(key) === task) pending.delete(key);
    });
    pending.set(key, task);
    return task;
  }
  return { read, load, invalidate };
}
