// 日期仍是事件信息，但已不参与当前账本内的事件身份。
export function eventIdentityPayload(form, excludeEventId = null) {
  if (!form.host_contact_id || !form.kinship_type_id || !form.event_type_id) return null;
  return {
    host_contact_id: Number(form.host_contact_id),
    kinship_type_id: Number(form.kinship_type_id),
    event_type_id: Number(form.event_type_id),
    remark: String(form.remark || '').trim() || null,
    ...(excludeEventId ? { exclude_event_id: Number(excludeEventId) } : {}),
  };
}

// 关闭、切换身份和退出登录均会使在途请求失效，旧结果不可进入新的表单。
export function createEventIdentityChecker(request) {
  let revision = 0;
  return {
    invalidate() { revision += 1; },
    async check(payload) {
      const current = ++revision;
      try {
        const result = payload ? await request(payload) : { existing_event: null };
        return { current: current === revision, existingEvent: result.existing_event || null };
      } catch (error) {
        return { current: current === revision, error };
      }
    },
  };
}

// 同一格式用于事件/记账列表、事件候选和已选摘要，追加的是事件备注。
export function eventDisplayTitle(row, contacts = []) {
  const event = row.event || row;
  const host = contacts.find(contact => contact.id === event.host.id);
  const identity = host && !host.is_self
    ? `(${[host.relationship_type_name, host.remark].filter(Boolean).join('·')})` : '';
  const kinship = event.kinship_type.name;
  const kinshipLabel = kinship.startsWith('之') ? kinship : ` · ${kinship}`;
  const remark = String(event.remark || '').trim();
  return `${event.host.name}${identity}${kinshipLabel} · ${event.event_type.name}${remark ? ' · ' + remark : ''}`;
}
