export function revisionReview(original, latest, draft, labels) {
  const merged = { ...draft };
  const changes = [];
  for (const [field, label] of Object.entries(labels)) {
    const equal = (a, b) => field === 'amount' ? Number(a) === Number(b) : a === b;
    if (equal(original[field], latest[field])) continue;
    const edited = !equal(original[field], draft[field]);
    const shown = (value) => value === '' || value == null ? '空' : String(value);
    changes.push(`${label}：当前「${shown(latest[field])}」${edited ? `，草稿「${shown(draft[field])}」` : ''}`);
    if (!edited) merged[field] = latest[field];
  }
  return { changes, merged };
}
