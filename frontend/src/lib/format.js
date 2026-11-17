export function money(value) {
  return '¥' + Number(value || 0).toLocaleString('zh-CN', {maximumFractionDigits: 2});
}
export function signedMoney(value) {
  const amount = Number(value);
  return (amount > 0 ? '+' : amount < 0 ? '−' : '') + money(Math.abs(amount));
}
export function balanceClass(value) {
  return Number(value) > 0 ? 'income' : Number(value) < 0 ? 'expense' : '';
}
export function stamp(value) {
  return value ? value.replace('T',' ').replaceAll('-','/').slice(0,19) : '';
}

/** 旧账日期保留实际精度，不能用月初或年初代替未知的日期。 */
export function eventDate(value) {
  const event = value?.event || value;
  if (typeof event === 'string') return event || '不详';
  if (!event) return '不详';
  if (event.event_date_display) return event.event_date_display;
  if (event.date_precision === 'unknown') return '不详';
  if (event.date_precision === 'year' && event.event_year) return String(event.event_year).padStart(4, '0');
  if (event.date_precision === 'month' && event.event_year && event.event_month) return `${String(event.event_year).padStart(4, '0')}-${String(event.event_month).padStart(2, '0')}`;
  return event.event_date || '不详';
}

export function isCompleteEventDate(value) {
  const event = value?.event || value;
  if (event && typeof event === 'object' && event.date_precision && event.date_precision !== 'day') return false;
  const date = typeof event === 'string' ? event : event?.event_date;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date || '')) return false;
  const [year, month, day] = date.split('-').map(Number);
  const actual = new Date(0);
  actual.setUTCFullYear(year, month - 1, day);
  actual.setUTCHours(0, 0, 0, 0);
  return year >= 1000 && year <= 9999 && actual.getUTCFullYear() === year && actual.getUTCMonth() === month - 1 && actual.getUTCDate() === day;
}

/** 补齐旧账时保留已有的年/月；完整日期的正常纠错不受此限制。 */
export function canCompleteEventDate(event, completeDate) {
  const source = event?.event || event;
  if (!isCompleteEventDate(completeDate)) return false;
  if (isCompleteEventDate(source)) return true;
  const known = eventDate(source);
  return known === '不详' || completeDate.startsWith(known + '-');
}
