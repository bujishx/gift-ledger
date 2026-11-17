import { nextTick, ref } from 'vue';

// 查询与位置仅保存在当前文档内存。浏览器历史只持有随机索引，不能持有账本资料或表单草稿。
export function usePageNavigation({ page, states, load, closeDetails, readDetails, restoreDetails,
  routeChanged = () => {}, confirmLeave = async () => true, onError = () => {} }) {
  const related = ref(null), restoring = ref(false);
  const browser = window, history = browser.history;
  const entries = new Map();
  let normal = {}, positions = {}, ready = false, changing = false, destroyed = false;
  let generation = 0, scope = '', sessionKey = null, currentEntry = null;
  let pendingPop = null, popRevision = 0;
  const clone = value => value == null ? value : JSON.parse(JSON.stringify(value));
  const nonce = () => browser.crypto?.randomUUID?.() || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  const alive = token => ready && !destroyed && token === generation;
  const validPage = id => typeof id === 'string' && Object.hasOwn(states, id);
  const position = () => ({
    y: document.body.style.position === 'fixed' ? -parseFloat(document.body.style.top || '0') : browser.scrollY,
    x: [...document.querySelectorAll('.content .table-scroller')].map(element => element.scrollLeft),
  });
  const detailId = () => {
    const detail = readDetails?.(), id = Number(detail?.id);
    return detail && ['people', 'events', 'ledger'].includes(detail.kind) && Number.isSafeInteger(id) && id > 0
      ? { kind: detail.kind, id } : null;
  };
  function read(id) {
    return { query: clone(states[id].read()), position: clone(id === page.value ? position() : positions[id] || { y: 0, x: [] }) };
  }
  function ensure() { for (const id of Object.keys(states)) normal[id] ||= read(id); }
  function snapshot() {
    positions[page.value] = position();
    return { page: page.value, view: read(page.value), related: clone(related.value), detail: detailId() };
  }
  function url(id) { const address = new URL(browser.location.href); address.searchParams.set('page', id); return address.href; }
  function remember() {
    if (!ready || destroyed || !validPage(page.value)) return;
    currentEntry ||= nonce();
    entries.set(currentEntry, snapshot());
    history.replaceState({ ledgerNavigation: { version: 1, scope, entry: currentEntry } }, '', url(page.value));
  }
  function push() {
    currentEntry = nonce(); entries.set(currentEntry, snapshot());
    history.pushState({ ledgerNavigation: { version: 1, scope, entry: currentEntry } }, '', url(page.value));
  }
  function reset() {
    generation++; ready = false; changing = false; restoring.value = false; related.value = null;
    pendingPop = null; popRevision++;
    sessionKey = null; scope = ''; currentEntry = null; normal = {}; positions = {}; entries.clear();
    closeDetails?.();
    if (history.state?.ledgerNavigation) history.replaceState(null, '', browser.location.href);
  }
  function init(key) {
    reset();
    if (destroyed || key === null || key === undefined || key === '' || !validPage(page.value)) return false;
    sessionKey = key; scope = nonce(); ready = true; ensure(); remember(); return true;
  }
  function saveNormal() { ensure(); if (!related.value) normal[page.value] = read(page.value); }
  async function apply(id, view, token) {
    if (!alive(token)) return false;
    restoring.value = true; page.value = id; closeDetails?.(); states[id].write(clone(view.query)); routeChanged();
    await nextTick(); if (!alive(token)) return false;
    await load(); if (!alive(token)) return false;
    await nextTick(); return alive(token);
  }
  async function restorePosition(saved = { y: 0, x: [] }, token) {
    await nextTick(); if (!alive(token)) return false;
    await new Promise(resolve => browser.requestAnimationFrame(() => browser.requestAnimationFrame(resolve)));
    if (!alive(token)) return false;
    browser.scrollTo({ top: Number.isFinite(saved.y) ? saved.y : 0, behavior: 'instant' });
    document.querySelectorAll('.content .table-scroller').forEach((element, index) => { element.scrollLeft = saved.x?.[index] || 0; });
    positions[page.value] = position(); return true;
  }
  async function run(action, { confirm = true, rememberHistory = true, recordEntry = true } = {}) {
    if (!ready || destroyed || changing || sessionKey === null) return false;
    const token = generation; changing = true;
    let before, successful = false, queued;
    try {
      if ((!confirm || await confirmLeave()) && alive(token)) {
        saveNormal(); before = snapshot();
        if (rememberHistory) remember();
        else if (recordEntry && currentEntry) entries.set(currentEntry, before);
        const result = await action(token);
        successful = alive(token) && result !== false;
      }
    } catch (error) {
      if (!alive(token)) return false;
      if (before) {
        related.value = clone(before.related);
        try {
          if (await apply(before.page, before.view, token) && await restorePosition(before.view.position, token)) {
            if (before.detail) await restoreDetails?.(before.detail);
          }
        } catch { /* 原请求错误只报告一次；查询快照已恢复，可由用户重试。 */ }
        if (alive(token)) remember();
      }
      if (alive(token)) onError(error);
    } finally {
      if (generation === token) {
        restoring.value = false; changing = false;
        queued = pendingPop; pendingPop = null;
      }
    }
    if (queued && alive(token)) { await pop({ navigationRequest: queued }); return false; }
    return successful;
  }
  async function navigate(id) {
    if (!validPage(id)) return false;
    return run(async token => {
      related.value = null;
      if (!await apply(id, normal[id], token) || !await restorePosition(normal[id].position, token)) return false;
      if (pendingPop) return false; push(); return true;
    });
  }
  async function associate(id, info, prepare, after) {
    if (!validPage(id)) return false;
    return run(async token => {
      const source = page.value; restoring.value = true; closeDetails?.(); await prepare();
      if (!alive(token)) return false;
      page.value = id;
      related.value = { ...clone(info), sourcePage: source, sourceLabel: { people: '联系人', events: '事件', ledger: '记账', stats: '统计', me: '我的' }[source] };
      routeChanged(); await nextTick(); if (!alive(token)) return false;
      await load(); if (!alive(token)) return false;
      if (!await restorePosition({ y: 0, x: [] }, token)) return false;
      restoring.value = false; if (after) await after(); if (!alive(token)) return false;
      if (pendingPop) return false; push(); return true;
    });
  }
  async function exit() {
    if (!related.value) return false;
    return run(async token => {
      const id = page.value; related.value = null;
      if (!await apply(id, normal[id], token) || !await restorePosition(normal[id].position, token)) return false;
      if (pendingPop) return false; push(); return true;
    });
  }
  function clear() { if (!ready || destroyed) return; related.value = null; normal[page.value] = read(page.value); remember(); }
  async function back() { if (!ready || destroyed || changing) return false; remember(); history.back(); return true; }
  async function pop(event) {
    if (!ready || destroyed) return false;
    const request = event.navigationRequest || { state: clone(event.state), url: browser.location.href, revision: ++popRevision };
    if (changing) { pendingPop = request; return false; }
    const marker = request.state?.ledgerNavigation;
    const saved = marker?.version === 1 && marker.scope === scope ? entries.get(marker.entry) : null;
    // 刷新或换账号后的旧历史只用于正常页面导航，不恢复上一会话的范围或详情。
    const requested = new URL(request.url).searchParams.get('page');
    const id = saved?.page || (validPage(requested) ? requested : page.value);
    const accepted = await run(async token => {
      related.value = saved ? clone(saved.related) : null;
      const view = saved?.view || normal[id];
      if (!await apply(id, view, token) || !await restorePosition(view.position, token)) return false;
      if (saved?.detail) await restoreDetails?.(saved.detail);
      if (!alive(token) || request.revision !== popRevision) return false;
      currentEntry = saved ? marker.entry : nonce(); remember(); return true;
    }, { rememberHistory: false, recordEntry: !event.navigationRequest });
    if (!accepted && ready && !destroyed && request.revision === popRevision) { currentEntry = nonce(); push(); }
    return accepted;
  }
  browser.addEventListener('popstate', pop);
  function destroy() { if (destroyed) return; reset(); destroyed = true; browser.removeEventListener('popstate', pop); }
  return { related, restoring, init, reset, navigate, associate, exit, clear, back, remember, destroy };
}
