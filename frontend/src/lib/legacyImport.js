/** 将服务端预检状态转为展示数据，业务匹配和确认校验始终由服务端执行。 */
export function importIssues(preview) {
  if (Array.isArray(preview?.issues)) return preview.issues;
  return (preview?.errors || []).map(issue => ({ ...issue, severity: 'error' }));
}

export function importIssueGroups(preview) {
  const issues = importIssues(preview);
  return ['error', 'confirmation', 'info'].map(severity => ({
    severity,
    label: { error: '需修改', confirmation: '待确认', info: '提示' }[severity],
    issues: issues.filter(issue => issue.severity === severity),
  }));
}

export function canImport(preview) {
  if (!preview?.preview_token) return false;
  if (Number(preview.error_rows || 0) || importIssues(preview).some(issue => ['error', 'confirmation'].includes(issue.severity))) return false;
  if (preview.can_import !== undefined) return preview.can_import === true;
  return true;
}

export function importChoicesComplete(preview, decisions) {
  const confirmations = importIssues(preview).filter(issue => issue.severity === 'confirmation');
  return confirmations.length > 0 && confirmations.every(issue => {
    const choice = decisions?.[issue.confirmation_key];
    return issue.confirmation_key && issue.options?.some(option => String(option.value) === String(choice));
  });
}

export function plannedAction(value) {
  return { create: '新增', new: '新增', reuse: '复用', reused: '复用', complete_date: '补齐原事件日期', update_date: '补齐原事件日期' }[value] || value || '待核对';
}

export function datePrecisionCounts(summary) {
  const counts = summary?.date_precision_counts || {};
  return [['day', '完整日期'], ['month', '仅年月'], ['year', '仅年份'], ['unknown', '日期不详']].map(([key, label]) => ({ key, label, value: Number(counts[key] || 0) }));
}

/** 决定只保留在当前登录内；任何决定改变，旧最终计划不能继续提交。 */
export function setImportDecision(state, key, value) {
  state.importDecisions = { ...(state.importDecisions || {}), [key]: value };
  state.importAcknowledged = false;
  state.importDecisionsDirty = true;
}
