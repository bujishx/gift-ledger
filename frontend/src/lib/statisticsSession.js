import { statisticsTab } from './statisticsView.js';
const PREFIX = 'gift_ledger_statistics_view_v1:';
const presets = new Set(['ytd','month','lastMonth','quarter','lastQuarter','30days','3months','12months','lastYear','year','selectedMonth','all','custom']);

const statuses = new Set(['self_unreturned','counterparty_unreturned','mutual','none']);
const uniqueIds = value => Array.isArray(value) ? [...new Set(value.map(Number).filter(id => Number.isSafeInteger(id) && id > 0))] : [];
export function statisticsViewState(value = {}, now = new Date()) {
  const result = {
    preset: 'all', year: now.getFullYear(), month: `${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,'0')}`,
    date_from: '', date_to: '', keyword: '', counterparty_relationship_type_ids: [], event_type_ids: [],
    interaction_statuses: [], date_basis: 'event', tab: 'overview', grain: 'auto',
  };
  if (!value || typeof value !== 'object') return result;
  if (presets.has(value.preset)) result.preset = value.preset;
  result.tab = statisticsTab(value.tab);
  if (/^\d{4}$/.test(String(value.year))) result.year = value.year;
  for (const key of ['month','date_from','date_to']) if (typeof value[key] === 'string' && value[key].length <= 10) result[key] = value[key];
  if (typeof value.keyword === 'string') result.keyword = value.keyword.trim().slice(0, 100);
  result.counterparty_relationship_type_ids = uniqueIds(value.counterparty_relationship_type_ids || (value.relationship_type_id ? [value.relationship_type_id] : []));
  result.event_type_ids = uniqueIds(value.event_type_ids || (value.event_type_id ? [value.event_type_id] : []));
  result.interaction_statuses = [...new Set((Array.isArray(value.interaction_statuses) ? value.interaction_statuses : value.interaction_status ? [value.interaction_status] : []).filter(status => statuses.has(status)))];
  if (value.date_basis === 'registered') result.date_basis = 'registered';
  if (['year', 'quarter', 'month'].includes(value.grain)) result.grain = value.grain;
  // 联系人、具体事件及旧内嵌筛选展开状态已删除，不能留下不可见范围。
  return result;
}

export async function statisticsStorageKey(userId, sessionMarker = '') {
  // 登录 CSRF 标记只用于计算会话指纹，不保存或输出原始凭据。
  let digest = null;
  try { if (sessionMarker && globalThis.crypto?.subtle) digest = await globalThis.crypto.subtle.digest('SHA-256', new TextEncoder().encode(sessionMarker)); } catch { /* 无加密接口时由登录/退出动作清除缓存。 */ }
  const fingerprint = digest ? [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2,'0')).join('') : 'local';
  return `${PREFIX}${userId}:${fingerprint}`;
}

export function readStatisticsView(key, storage) {
  try { return statisticsViewState(JSON.parse((storage || globalThis.sessionStorage).getItem(key) || 'null')); }
  catch { return statisticsViewState(); }
}

export function writeStatisticsView(key, value, storage) {
  try { if (key) (storage || globalThis.sessionStorage).setItem(key, JSON.stringify(statisticsViewState(value))); } catch { /* 内存状态仍由 App 保留。 */ }
}

export function clearStatisticsViews(storage) {
  try {
    storage ||= globalThis.sessionStorage;
    const keys = Array.from({length: storage.length}, (_, index) => storage.key(index));
    keys.filter(key => key?.startsWith(PREFIX)).forEach(key => storage.removeItem(key));
  } catch { /* 禁止存储的浏览器仍可正常登录和筛选。 */ }
}
