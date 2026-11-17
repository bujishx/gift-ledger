export function buildFilterChips(filters, definitions, dateLabel) {
  const chips = definitions.flatMap(({key, label, options}) => (filters[key] || []).map(value => ({
    key, value, label: `${label}：${options.find(item => String(item.value) === String(value))?.label ?? value}`,
  })));
  if (filters.date_from || filters.date_to) chips.push({
    key: 'date', label: `${dateLabel}：${filters.date_from || '不限'} 至 ${filters.date_to || '不限'}`,
  });
  return chips;
}

export function removeFilterChip(filters, chip) {
  if (chip.key === 'date') {
    filters.date_from = '';
    filters.date_to = '';
  } else {
    filters[chip.key] = filters[chip.key].filter(value => value !== chip.value);
  }
}
