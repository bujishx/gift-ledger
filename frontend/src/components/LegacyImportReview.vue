<script setup>
import { computed, ref, watch } from 'vue';
import { money, stamp } from '../lib/format.js';
import { canImport, importChoicesComplete, importIssueGroups, plannedAction, datePrecisionCounts } from '../lib/legacyImport.js';

const props = defineProps({
  preview: { type: Object, required: true },
  decisions: { type: Object, default: () => ({}) },
  dirty: Boolean,
  acknowledged: Boolean,
  busy: Boolean,
});
const emit = defineEmits(['decision', 'recheck', 'update:acknowledged', 'confirm', 'view-issue', 'edit-issue']);
const groups = computed(() => importIssueGroups(props.preview));
const ready = computed(() => canImport(props.preview) && !props.dirty);
const choicesComplete = computed(() => importChoicesComplete(props.preview, props.decisions));
const precisionCounts = computed(() => datePrecisionCounts(props.preview.summary));
const reportUrl = computed(() => `/api/v1/imports/${encodeURIComponent(props.preview.preview_token)}/issues.xlsx`);
const planPage = ref(1), planPageSize = 50;
const planPages = computed(() => Math.max(1, Math.ceil((props.preview.rows?.length || 0) / planPageSize)));
const visiblePlans = computed(() => (props.preview.rows || []).slice((planPage.value - 1) * planPageSize, planPage.value * planPageSize));
watch(() => props.preview, () => { planPage.value = 1; });
</script>

<template>
  <div class="legacy-import-review">
    <div class="import-review-heading"><h3>预检结果</h3><a v-if="preview.preview_token" class="outline link-button" :href="reportUrl">下载问题报告</a></div>
    <p class="me-summary">共 <b>{{ preview.total_rows }}</b> 行；有效 <b class="income">{{ preview.valid_rows }}</b> 行；需修改 <b :class="preview.error_rows ? 'expense' : ''">{{ preview.error_rows }}</b> 行。预检和选择确认都不会写入账本。</p>
    <section v-for="group in groups.filter(item => item.issues.length)" :key="group.severity" class="import-issue-group" :class="group.severity" :aria-label="group.label">
      <h4>{{ group.label }} · {{ group.issues.length }} 项</h4>
      <p v-if="group.severity === 'error'" class="muted">请按原 Excel 行号修改文件，再上传预检。整批通过前不会导入。</p>
      <p v-if="group.severity === 'confirmation'" class="muted">逐项选择后，点击“按选择重新预检”，再核对服务器生成的最终计划。</p>
      <ol>
        <li v-for="(issue,index) in group.issues" :key="issue.confirmation_key || `${issue.row_number}-${issue.code}-${index}`">
          <b>{{ issue.row_number ? `Excel 第 ${issue.row_number} 行` : '文件' }}{{ issue.field ? ` · ${issue.field}` : '' }}</b>
          <span>{{ issue.message }}</span>
          <small v-if="issue.raw_value !== null && issue.raw_value !== undefined && issue.raw_value !== ''">原填写：{{ issue.raw_value }}</small>
          <small v-if="issue.remedy">处理建议：{{ issue.remedy }}</small>
          <div v-if="issue.existing_event_id || issue.existing_gift_id" class="form-actions"><button type="button" class="outline" :disabled="busy" @click="emit('view-issue',issue)">查看</button><button type="button" class="soft-primary" :disabled="busy" @click="emit('edit-issue',issue)">修改</button></div>
          <label v-if="group.severity === 'confirmation' && issue.confirmation_key && issue.options?.length" class="field">
            <span class="field-label">请选择处理方式</span>
            <select :value="decisions[issue.confirmation_key] ?? ''" :disabled="busy" @change="emit('decision', issue.confirmation_key, $event.target.value)">
              <option value="">请选择，尚未确认</option>
              <option v-for="option in issue.options" :key="option.value" :value="option.value">{{ option.label }}</option>
            </select>
          </label>
        </li>
      </ol>
    </section>
    <div v-if="groups[1].issues.length || dirty" class="form-actions me-actions">
      <button type="button" class="outline" :disabled="busy || (!dirty && !choicesComplete) || (groups[1].issues.length > 0 && !choicesComplete)" @click="emit('recheck')">按选择重新预检</button>
    </div>
    <p v-if="dirty" class="muted" role="status">处理选择已改变。请重新预检；下方计划尚未更新，不能直接确认导入。</p>

    <section v-if="preview.summary" class="import-plan-summary" aria-label="新增收支汇总">
      <h4>本批计划新增</h4>
      <div class="preview-counts">
        <span>收入 {{ preview.summary.income_count || 0 }} 笔 <b class="income">{{ money(preview.summary.income_amount) }}</b></span>
        <span>支出 {{ preview.summary.expense_count || 0 }} 笔 <b class="expense">{{ money(preview.summary.expense_amount) }}</b></span>
        <span>其中纯实物 <b>{{ preview.summary.pure_goods_count || 0 }} 笔</b></span>
        <span>新增联系人 <b>{{ preview.new_contacts?.length || 0 }} 位</b></span>
        <span>复用联系人 <b>{{ preview.reused_contact_count || 0 }} 位</b></span>
        <span>新增个人字典 <b>{{ preview.new_dictionary_items?.length || 0 }} 项</b></span>
        <span>新增事件 <b>{{ preview.new_event_count || 0 }} 个</b></span>
        <span>复用事件 <b>{{ preview.reused_event_count || 0 }} 个</b></span>
        <span>补齐原事件日期 <b>{{ preview.completed_date_event_count || 0 }} 个</b></span>
      </div>
      <p class="muted me-field-note">纯实物包含在收入／支出笔数中，不再重复计数；日期精度按事件统计，日期不详的事件保留原状态。</p>
      <div class="import-date-counts"><span v-for="item in precisionCounts" :key="item.key">{{ item.label }} {{ item.value }} 个</span></div>
    </section>
    <div v-if="preview.new_dictionary_items?.length" class="me-detail-list">
      <h4>将新增的个人字典项</h4>
      <p v-for="(item,index) in preview.new_dictionary_items" :key="index">{{ { relationship:'关系类型',kinship:'亲属称谓',event:'事件类型' }[item.category] || item.category }}：{{ item.name }}</p>
    </div>
    <details v-if="preview.rows?.length" class="import-row-plans" :open="ready">
      <summary>逐行导入计划（{{ preview.rows.length }} 行）</summary>
      <div v-if="planPages > 1" class="form-actions import-plan-pagination"><span>每页 {{ planPageSize }} 行 · 第 {{ planPage }}／{{ planPages }} 页</span><button type="button" class="outline" :disabled="planPage === 1 || busy" @click="planPage--">上一页</button><button type="button" class="outline" :disabled="planPage === planPages || busy" @click="planPage++">下一页</button></div>
      <div class="import-plan-scroll"><table class="data-table"><thead><tr><th>原 Excel 行号</th><th>事主事件／日期</th><th>随礼人</th><th>收支及实物</th><th>处理结果</th></tr></thead><tbody>
        <tr v-for="row in visiblePlans" :key="row.row_number">
          <td>第 {{ row.row_number }} 行</td>
          <td><strong>{{ row.event_name }}</strong><small>办事日期 {{ row.event_date || '不详' }}</small><small>事主：{{ row.host_name }} · {{ row.host_relationship }}</small></td>
          <td>{{ row.giver_name }}<small>{{ row.giver_relationship }}</small></td>
          <td><span :class="row.direction === 'income' ? 'income' : 'expense'">{{ row.direction === 'income' ? '收入' : '支出' }} {{ money(row.amount) }}</span><small>实物：{{ row.gift_details || '—' }}</small><small>流水备注：{{ row.gift_remark || '—' }}</small></td>
          <td><small>事主：{{ plannedAction(row.host_action) }}</small><small>随礼人：{{ plannedAction(row.giver_action) }}</small><small>事件：{{ plannedAction(row.event_action) }}</small></td>
        </tr>
      </tbody></table></div>
    </details>
    <p v-if="preview.expires_at" class="muted me-field-note">预检有效至 {{ stamp(preview.expires_at) }}。确认前会再次检查账本；若原资料有变化，需要重新预检。</p>
    <template v-if="ready">
      <label class="confirm-check"><input :checked="acknowledged" type="checkbox" :disabled="busy" @change="emit('update:acknowledged', $event.target.checked)" /><span>我已核对逐行计划、收支金额和处理决定，同意一次导入整批记录。</span></label>
      <div class="form-actions me-actions"><button type="button" class="primary" :disabled="busy || !acknowledged" @click="emit('confirm')">确认导入这些记录</button></div>
    </template>
  </div>
</template>
