<script>
/**
 * “我的”页面的纯展示逻辑（U11）。放在普通 <script> 块里以便针对性测试直接引用，
 * 不接触业务动作、不改任何 state 字段与 actions 名称。
 */
export const ME_SECTIONS = [
  { id: "data", label: "资料与导出" },
  { id: "import", label: "旧账导入" },
  { id: "restore", label: "备份恢复" },
  { id: "dictionary", label: "字典管理" },
];

export const ME_IMPORT_STEPS = ["选择文件", "上传预检", "核对结果", "确认导入"];
export const ME_RESTORE_STEPS = ["选择文件", "上传预检", "核对替换范围", "密码确认"];

export const ME_DICTIONARY_LABELS = {
  relationship: "关系类型",
  kinship: "亲属称谓",
  event: "事件类型",
};

export const ME_RESTORE_ROWS = [
  ["contacts", "联系人"],
  ["events", "事件"],
  ["gifts", "记账记录"],
  ["relationship_types", "关系类型"],
  ["kinship_types", "亲属称谓"],
  ["event_types", "事件类型"],
];

function meStepList(labels, index) {
  return labels.map((label, position) => ({
    label,
    state: position < index ? "done" : position === index ? "current" : "todo",
  }));
}

/** 导入旧账的阶段：0 选择文件／1 上传预检／2 核对结果／3 确认导入。 */
export function meImportStepIndex({ file = null, preview = null, dirty = false } = {}) {
  if (!file) return 0;
  if (!preview) return 1;
  return meImportHasErrors(preview) || preview.can_import === false || dirty ? 2 : 3;
}

/** 备份恢复的阶段：0 选择文件／1 上传预检／2 核对替换范围／3 密码确认。 */
export function meRestoreStepIndex({
  file = null,
  preview = null,
  acknowledged = false,
} = {}) {
  if (!file) return 0;
  if (!preview) return 1;
  return acknowledged ? 3 : 2;
}

export function meImportStepList(context = {}) {
  return meStepList(ME_IMPORT_STEPS, meImportStepIndex(context));
}

export function meRestoreStepList(context = {}) {
  return meStepList(ME_RESTORE_STEPS, meRestoreStepIndex(context));
}

/** 预检结果里是否存在必须修改后才能导入的错误行。 */
export function meImportHasErrors(preview) {
  if (!preview) return false;
  return Number(preview.error_rows || 0) > 0 || (preview.errors?.length || 0) > 0;
}

/** 预检通过后的核对数量（含复用事件，信息不减少）。 */
export function meImportCounts(preview) {
  if (!preview) return [];
  return [
    { label: "新增联系人", value: preview.new_contacts?.length || 0 },
    { label: "复用联系人", value: preview.reused_contact_count || 0 },
    { label: "新增字典项", value: preview.new_dictionary_items?.length || 0 },
    { label: "新增事件", value: preview.new_event_count || 0 },
    { label: "复用事件", value: preview.reused_event_count || 0 },
    { label: "新增记账记录", value: preview.new_gift_count || 0 },
  ];
}

/** 预览中即将新增的联系人明细。 */
export function meImportContacts(preview) {
  return (preview?.new_contacts || []).map((row) => ({
    name: row.name,
    detail: [row.relationship_type, row.location, row.remark]
      .filter(Boolean)
      .join(" · "),
  }));
}

/** 预览中即将新增的自定义字典项明细。 */
export function meImportDictionaryItems(preview) {
  return (preview?.new_dictionary_items || []).map((row) => ({
    name: row.name,
    detail: ME_DICTIONARY_LABELS[row.category] || row.category,
  }));
}

/** 替换范围表：当前数量与恢复后数量逐项对照。 */
export function meRestoreCountRows(preview) {
  if (!preview?.current || !preview?.incoming) return [];
  return ME_RESTORE_ROWS.map(([key, label]) => ({
    key,
    label,
    current: preview.current[key],
    incoming: preview.incoming[key],
  }));
}

export function meDictionaryLabel(category) {
  return ME_DICTIONARY_LABELS[category] || "";
}

export const ME_DICTIONARY_MENU = [
  { id: "rename", label: "修改名称" },
  { id: "toggle", label: "启用／停用" },
  { id: "delete", label: "删除", danger: true },
];
</script>

<script setup>
import { computed, onMounted, ref, toRefs } from 'vue';
import { money, stamp } from '../lib/format.js';
import { datePrecisionCounts } from '../lib/legacyImport.js';
import SiteAbout from '../components/SiteAbout.vue';
import MeWorkflowSteps from './MeWorkflowSteps.vue';
import RowMenu from '../components/RowMenu.vue';
import LegacyImportReview from '../components/LegacyImportReview.vue';

const props = defineProps({ state: { type: Object, required: true }, actions: { type: Object, required: true }, maintenanceAvailable: Boolean });
const {
  profileForm,
  meSection,
  busy,
  dictionaries,
  dictionaryCategory,
  dictionaryDraft,
  dictionaryRename,
  importFile,
  importPreview,
  restoreFile,
  restorePreview,
  restorePassword,
  restoreAcknowledged,
  restoreRenames,
  safetyBackupToken,
} = toRefs(props.state);
const {
  saveMyProfile,
  previewOldLedger,
  confirmOldLedger,
  previewRestore,
  confirmRestore,
  createDictionaryItem,
  saveDictionaryRename,
  cancelDictionaryRename,
  setDictionaryActive,
  deleteDictionary,
  logout,
} = props.actions;

const meSections = computed(() => ME_SECTIONS.filter(section => props.maintenanceAvailable || section.id !== 'restore'));
const activeSection = computed(() =>
  meSection.value === 'restore' && !props.maintenanceAvailable ? 'data' : meSection.value
);
const error = computed(() => props.state.error || '');
const notice = computed(() => props.state.notice || '');
function clearFeedback() {
  if ('error' in props.state) props.state.error = '';
  if ('notice' in props.state) props.state.notice = '';
}
function selectSection(id) {
  if (busy.value) return;
  clearFeedback();
  meSection.value = id;
  if (id === 'import') props.actions.refreshImportBatches?.();
}
onMounted(() => { if (meSection.value === 'import') props.actions.refreshImportBatches?.(); });
const dictionaryLabels = ME_DICTIONARY_LABELS;
const dictionaryCategories = Object.entries(ME_DICTIONARY_LABELS).map(([id, label]) => ({ id, label }));

/* 本人资料表单：saveMyProfile 没有业务层忙状态，这里用页面内部状态给出保存中／禁用，
   只包一层 await，不改变 state 字段或 actions 名称。 */
const savingProfile = ref(false);
async function submitProfile() {
  if (savingProfile.value) return;
  savingProfile.value = true;
  try {
    await saveMyProfile();
  } finally {
    savingProfile.value = false;
  }
}

const importSteps = computed(() => meImportStepList({ file: importFile.value, preview: importPreview.value, dirty:props.state.importDecisionsDirty }));
const restoreSteps = computed(() =>
  meRestoreStepList({
    file: restoreFile.value,
    preview: restorePreview.value,
    acknowledged: restoreAcknowledged.value,
  }),
);
const importHasErrors = computed(() => meImportHasErrors(importPreview.value));
const importCounts = computed(() => meImportCounts(importPreview.value));
const importContacts = computed(() => meImportContacts(importPreview.value));
const importDictionaryItems = computed(() => meImportDictionaryItems(importPreview.value));
const restoreCountRows = computed(() => meRestoreCountRows(restorePreview.value));
const dictionaryItems = computed(() => dictionaries.value?.[dictionaryCategory.value] || []);
const importFileInput = ref(null), restoreFileInput = ref(null);
const versionInfoOpen = ref(false);

function clearImportFile() {
  if (busy.value || props.state.importResultUncertain) return;
  clearFeedback();
  importFile.value = null;
  importPreview.value = null;
  clearImportChoices();
  if (importFileInput.value) importFileInput.value.value = '';
}
function clearRestoreFile() {
  if (busy.value) return;
  clearFeedback();
  restoreFile.value = null;
  restorePreview.value = null;
  restorePassword.value = '';
  restoreAcknowledged.value = false;
  restoreRenames.value = [];
  if (restoreFileInput.value) restoreFileInput.value.value = '';
}

function onImportFileChange(event) {
  if (props.state.importResultUncertain) return;
  clearFeedback();
  importFile.value = event.target.files?.[0] || null;
  importPreview.value = null;
  clearImportChoices();
}
function clearImportChoices() {
  props.state.importDecisions = {};
  props.state.importDecisionsDirty = false;
  props.state.importAcknowledged = false;
  props.state.importResult = null;
}
function onRestoreFileChange(event) {
  clearFeedback();
  restoreFile.value = event.target.files?.[0] || null;
  restorePreview.value = null;
  restorePassword.value = '';
  restoreAcknowledged.value = false;
  restoreRenames.value = [];
}
async function selectDictionaryCategory(category) {
  if (busy.value) return;
  if (category === dictionaryCategory.value) return;
  const editing = dictionaries.value[dictionaryCategory.value].find(item => item.id === dictionaryRename.value.id);
  if (editing && !(await cancelDictionaryRename(editing))) return;
  clearFeedback();
  dictionaryCategory.value = category;
  dictionaryRename.value.id = null;
}
function dictionaryMenuItemsFor(item) {
  return ME_DICTIONARY_MENU.map((entry) =>
    entry.id === "toggle"
      ? { ...entry, label: item.is_active ? "停用" : "启用" }
      : entry,
  );
}
function onDictionaryMenu(item, action) {
  if (action === "rename") {
    dictionaryRename.value.id = item.id;
    dictionaryRename.value.name = item.name;
    return;
  }
  if (action === "toggle") {
    setDictionaryActive(item);
    return;
  }
  if (action === "delete") deleteDictionary(item);
}
</script>

<template>
  <section class="me-page">
    <div class="page-heading">
      <div>
        <h1>我的</h1>
        <p>管理本人资料和个人数据</p>
      </div>
    </div>

    <div class="tab-strip subpage-tabs me-tabs" role="tablist" aria-label="我的子页">
      <button
        v-for="section in meSections"
        :key="section.id"
        type="button"
        role="tab"
        :aria-selected="activeSection === section.id"
        :class="{ selected: activeSection === section.id }"
        :disabled="busy"
        @click="selectSection(section.id)"
      >
        {{ section.label }}
      </button>
    </div>

    <div class="me-feedback" aria-live="polite">
      <p v-if="error" class="alert error" role="alert">{{ error }}</p>
      <p v-else-if="notice" class="alert success" role="status">{{ notice }}</p>
    </div>

    <!-- 资料与导出 -->
    <div v-if="activeSection === 'data'" class="me-grid">
      <div class="card form-card">
        <h2>本人信息</h2>
        <p class="muted">账本收支以这一位本人联系人自动判定。</p>
        <form class="form-stack" @submit.prevent="submitProfile">
          <label class="field">
            <span class="field-label"
              >真实姓名 <em class="me-required">必填</em></span
            >
            <input
              v-model="profileForm.name"
              name="name"
              autocomplete="name"
              required
              maxlength="50"
              placeholder="请填写真实姓名"
              :disabled="busy || savingProfile"
            />
          </label>
          <p class="muted me-field-note">
            姓名限 1～50 个字；保存后同步更新本人联系人姓名。
          </p>
          <div class="form-actions">
            <button
              class="primary"
              type="submit"
              :disabled="busy || savingProfile"
              :aria-busy="savingProfile"
            >
              {{ savingProfile ? "保存中…" : "保存本人信息" }}
            </button>
          </div>
        </form>
      </div>
      <div class="card form-card">
        <h2>我的数据</h2>
        <p class="muted">只包含当前账号的数据。</p>
        <div class="me-resource-list">
          <div class="me-resource">
            <button type="button" class="outline link-button" :disabled="busy" @click="actions.exportAllData">导出全部数据（Excel）</button>
            <small>联系人、事件、记账记录与自定义字典项的完整表格；下载前验证当前账号密码。</small>
          </div>
          <div v-if="maintenanceAvailable" class="me-resource">
            <button type="button" class="outline link-button" :disabled="busy" @click="actions.downloadPersonalBackup">下载个人账本备份</button>
            <small>.giftledger 备份，可在“备份恢复”中还原；下载前验证当前账号密码。</small>
          </div>
          <div class="me-resource">
            <a
              class="outline link-button"
              href="/api/v1/imports/template.xlsx"
              >下载旧账导入模板</a
            >
            <small>按模板填写旧账后，到“旧账导入”上传并预检。</small>
          </div>
        </div>
      </div>
    </div>

    <!-- 导入旧账 -->
    <div
      v-else-if="activeSection === 'import'"
      class="card form-card me-workflow"
    >
      <h2>导入旧账</h2>
      <p class="muted">
        按下载模板的“填写说明”逐行填写；标记必填／选填，字典列可下拉选择。上传预检不会写入数据，核对整批计划后，验证当前账号密码再确认导入。
      </p>
      <p class="muted me-field-note">“我”填写本人信息中的真实姓名，关系类型填“我”；事主和随礼人有且只有一方是本人。旧账办事日期可填完整日期、仅年月、仅年份或“不详”，网页新增仍须完整日期。</p>
      <MeWorkflowSteps :steps="importSteps" :busy="busy" label="导入旧账阶段" />
      <p v-if="busy" class="me-status" role="status">
        正在与服务器处理，请勿关闭页面…
      </p>
      <a class="outline link-button" href="/api/v1/imports/template.xlsx"
        >下载旧账导入模板</a
      >
      <label class="field">
        <span class="field-label">选择 .xlsx 文件</span>
        <input
          ref="importFileInput"
          type="file"
          accept=".xlsx"
          :disabled="busy || state.importResultUncertain"
          @change="onImportFileChange"
        />
      </label>
      <div v-if="importFile" class="me-selected-file" role="status">
        <p>已保留文件：<strong>{{ importFile.name }}</strong></p>
        <button type="button" class="text-button" :disabled="busy || state.importResultUncertain" @click="clearImportFile">清除所选文件</button>
        <small>切换页面后可继续使用此文件，无需重新选择。</small>
      </div>
      <div class="form-actions me-actions">
        <button
          class="primary"
          :disabled="busy || !importFile || state.importResultUncertain"
          @click="previewOldLedger"
        >
          上传并预检
        </button>
      </div>
      <section v-if="state.importResultUncertain" class="import-recovery" role="status">
        <h3>查询本批导入结果</h3>
        <p>本批确认结果尚未查明。请查询服务器保存的结果，核实前不要重复上传。</p>
        <div class="form-actions"><button type="button" class="outline" :disabled="busy" @click="actions.queryImportResult">查询本批结果</button><button v-if="state.importResult?.status === 'pending'" type="button" class="primary" :disabled="busy" @click="actions.retryImportConfirmation">继续确认本批</button><button v-if="state.importRecoveryUnavailable" type="button" class="outline" :disabled="busy" @click="actions.returnImportToPreview">核对后返回预检</button></div>
      </section>
      <section v-else-if="state.importResult?.batch_id" class="import-completed" role="status">
        <h3>本批已完成</h3><p>新增 {{ state.importResult.imported_contacts }} 位联系人、{{ state.importResult.imported_events }} 个事件、{{ state.importResult.imported_gifts }} 笔记账；补齐 {{ state.importResult.completed_date_events || 0 }} 个原事件日期。</p><small>批次编号：{{ state.importResult.batch_id }}</small>
        <p v-if="state.importResult.summary">实际新增收入 {{ state.importResult.summary.income_count }} 笔 · <span class="income">{{ money(state.importResult.summary.income_amount) }}</span>；支出 {{ state.importResult.summary.expense_count }} 笔 · <span class="expense">{{ money(state.importResult.summary.expense_amount) }}</span>；其中纯实物 {{ state.importResult.summary.pure_goods_count }} 笔。</p>
        <div v-if="state.importResult.summary" class="import-date-counts"><span v-for="item in datePrecisionCounts(state.importResult.summary)" :key="item.key">{{ item.label }} {{ item.value }} 个事件</span></div>
      </section>
      <LegacyImportReview v-if="importPreview && Array.isArray(importPreview.issues)" class="preview-block" :preview="importPreview" :decisions="state.importDecisions || {}" :dirty="state.importDecisionsDirty" :acknowledged="state.importAcknowledged" :busy="busy || state.importResultUncertain" @decision="actions.changeImportDecision" @recheck="previewOldLedger" @update:acknowledged="state.importAcknowledged = $event" @confirm="confirmOldLedger" @view-issue="actions.openImportIssue($event,false)" @edit-issue="actions.openImportIssue($event,true)" />
      <div v-else-if="importPreview" class="preview-block">
        <h3>预检结果</h3>
        <p class="me-summary">
          共 <b>{{ importPreview.total_rows }}</b> 行；有效
          <b class="income">{{ importPreview.valid_rows }}</b> 行；错误
          <b :class="importPreview.error_rows ? 'expense' : ''">{{
            importPreview.error_rows
          }}</b>
          行。
        </p>
        <template v-if="importHasErrors">
          <div class="issue-list" role="alert">
            <p class="me-issue-title">
              以下问题必须修改后再重新上传预检，当前内容不会写入账本：
            </p>
            <ol>
              <li v-for="(issue, index) in importPreview.errors" :key="index">
                <b
                  >{{ issue.row_number ? `第 ${issue.row_number} 行` : "文件"
                  }}{{ issue.field ? ` · ${issue.field}` : "" }}</b
                ><span>{{ issue.message }}</span>
              </li>
            </ol>
          </div>
          <small v-if="importPreview.errors_truncated" class="me-truncated"
            >只显示部分错误，请修改后重新预检。</small
          >
        </template>
        <template v-else>
          <div class="preview-counts">
            <span v-for="count in importCounts" :key="count.label"
              >{{ count.label }} <b>{{ count.value }}</b></span
            >
          </div>
          <div v-if="importContacts.length" class="me-detail-list">
            <h4>将新增的联系人</h4>
            <p v-for="(contact, index) in importContacts" :key="index">
              {{ contact.name }}
              <small v-if="contact.detail">· {{ contact.detail }}</small>
            </p>
          </div>
          <div v-if="importDictionaryItems.length" class="me-detail-list">
            <h4>将新增的自定义字典项</h4>
            <p v-for="(item, index) in importDictionaryItems" :key="index">
              {{ item.detail }}：{{ item.name }}
            </p>
          </div>
          <p class="muted">
            预览凭证有效至 {{ stamp(importPreview.expires_at) }}；确认为一次性操作，确认前不会写入任何数据。
          </p>
          <div class="form-actions me-actions">
            <button
              class="primary"
              :disabled="busy || !importPreview.preview_token"
              @click="confirmOldLedger"
            >
              确认导入这些记录
            </button>
          </div>
        </template>
      </div>
      <section class="import-batch-history" aria-label="最近导入记录">
        <div class="import-review-heading"><h3>最近导入记录</h3><button type="button" class="outline" :disabled="busy || state.importBatchLoading" @click="actions.refreshImportBatches">{{ state.importBatchLoading ? '查询中…' : '刷新记录' }}</button></div>
        <p v-if="state.importBatchError" class="muted" role="status">{{ state.importBatchError }}，请重试查询。</p>
        <ol v-if="state.importBatches?.length" class="import-batch-list"><li v-for="batch in state.importBatches" :key="batch.batch_id"><strong>{{ stamp(batch.completed_at) }} · 已完成 {{ batch.imported_gifts }} 笔记账</strong><small>新增联系人 {{ batch.imported_contacts }} 位，事件 {{ batch.imported_events }} 个，补齐日期 {{ batch.completed_date_events || 0 }} 个</small><small v-if="batch.summary">实际收入 {{ batch.summary.income_count }} 笔 · {{ money(batch.summary.income_amount) }}；支出 {{ batch.summary.expense_count }} 笔 · {{ money(batch.summary.expense_amount) }}；其中纯实物 {{ batch.summary.pure_goods_count }} 笔。</small><small>批次编号：{{ batch.batch_id }}</small></li></ol>
        <p v-else-if="!state.importBatchLoading" class="muted me-field-note">暂无已完成的导入批次。</p>
      </section>
    </div>

    <!-- 备份恢复 -->
    <div
      v-else-if="activeSection === 'restore'"
      class="card form-card me-workflow"
    >
      <h2>恢复个人备份</h2>
      <p class="alert error">
        恢复会用备份内容完整替换当前账本，无法与现有账本合并。请先下载当前个人备份。
      </p>
      <MeWorkflowSteps :steps="restoreSteps" :busy="busy" label="备份恢复阶段" />
      <p v-if="busy" class="me-status" role="status">
        正在与服务器处理，请勿关闭页面…
      </p>
      <button type="button" class="outline link-button" :disabled="busy" @click="actions.downloadPersonalBackup">下载当前个人备份</button>
      <button
        v-if="safetyBackupToken"
        type="button"
        class="outline link-button"
        :disabled="busy"
        @click="actions.downloadSafetyBackup"
        >下载上次恢复前安全备份</button
      >
      <label class="field">
        <span class="field-label">选择 .giftledger 备份文件</span>
        <input
          ref="restoreFileInput"
          type="file"
          accept=".giftledger"
          :disabled="busy"
          @change="onRestoreFileChange"
        />
      </label>
      <div v-if="restoreFile" class="me-selected-file" role="status">
        <p>已保留文件：<strong>{{ restoreFile.name }}</strong></p>
        <button type="button" class="text-button" :disabled="busy" @click="clearRestoreFile">清除所选文件</button>
        <small>切换页面后可继续使用此文件，无需重新选择。</small>
      </div>
      <div class="form-actions me-actions">
        <button
          class="outline"
          :disabled="busy || !restoreFile"
          @click="previewRestore"
        >
          上传并预检备份
        </button>
      </div>
      <div v-if="restorePreview" class="preview-block">
        <h3>替换范围</h3>
        <p>
          备份建立于 {{ stamp(restorePreview.created_at) }}；版本
          {{ restorePreview.application_version }}；备份格式 v{{
            restorePreview.format_version
          }}；预览有效至 {{ stamp(restorePreview.expires_at) }}
        </p>
        <p v-if="restorePreview.replace_notice" class="alert error me-replace-notice">
          {{ restorePreview.replace_notice }}
        </p>
        <div class="restore-table">
          <div>
            <strong>数据</strong><strong>当前</strong><strong>恢复后</strong>
          </div>
          <div v-for="row in restoreCountRows" :key="row.key">
            <span>{{ row.label }}</span
            ><span>{{ row.current }}</span
            ><span>{{ row.incoming }}</span>
          </div>
        </div>
        <div v-if="restoreRenames.length" class="rename-list">
          <h3>自定义字典名称冲突</h3>
          <p class="muted me-field-note">
            与被恢复备份重名的自定义字典项需要改用一个新名称，改名前无法确认恢复。
          </p>
          <label
            v-for="(item, index) in restoreRenames"
            :key="index"
            class="field"
          >
            <span class="field-label"
              >{{ dictionaryLabels[item.category] }}：{{ item.original_name }}
              <em class="me-required">必填</em></span
            >
            <input
              v-model="item.new_name"
              :disabled="busy"
              maxlength="50"
              placeholder="请填新名称"
            />
          </label>
        </div>
        <label class="confirm-check">
          <input v-model="restoreAcknowledged" type="checkbox" :disabled="busy" />
          <span>我已核对备份内容，了解当前账本将被完整替换。</span>
        </label>
        <label class="field">
          <span class="field-label"
            >当前账号密码 <em class="me-required">必填</em></span
          >
          <input
            v-model="restorePassword"
            :disabled="busy"
            type="password"
            autocomplete="current-password"
            placeholder="恢复前需再次验证密码"
          />
        </label>
        <p class="muted me-field-note">
          密码只用于本次恢复校验，不会保存在浏览器；恢复成功后需要重新登录，并可在“我的”下载恢复前安全备份。
        </p>
        <div class="form-actions me-actions">
          <button
            class="primary"
            :disabled="busy || !restoreAcknowledged || !restorePassword"
            @click="confirmRestore"
          >
            确认替换恢复
          </button>
        </div>
      </div>
    </div>

    <!-- 字典管理 -->
    <div v-else class="card form-card me-workflow">
      <h2>字典管理</h2>
      <p class="muted">
        系统内置项供所有人使用，不能修改或删除；你添加的自定义项仅属于自己的账本。
      </p>
      <div class="tab-strip me-dict-tabs" role="tablist" aria-label="字典分类">
        <button
          v-for="category in dictionaryCategories"
          :key="category.id"
          type="button"
          role="tab"
          :aria-selected="dictionaryCategory === category.id"
          :class="{ selected: dictionaryCategory === category.id }"
          :disabled="busy"
          @click="selectDictionaryCategory(category.id)"
        >
          {{ category.label }}
        </button>
      </div>
      <form class="dictionary-add" @submit.prevent="createDictionaryItem">
        <input
          v-model="dictionaryDraft"
          maxlength="50"
          :disabled="busy"
          :placeholder="`新建自定义${dictionaryLabels[dictionaryCategory]}`"
        />
        <button class="primary" :disabled="busy || !dictionaryDraft.trim()">
          ＋ 添加
        </button>
      </form>
      <p class="muted me-field-note">
        自定义项名称限 1～50 个字；停用后不再出现在新建选项里，已有记录不受影响。
      </p>
      <div class="dictionary-list">
        <div
          v-for="item in dictionaryItems"
          :key="item.id"
          class="dictionary-row"
        >
          <div class="me-dict-main">
            <template v-if="dictionaryRename.id === item.id">
              <input
                v-model="dictionaryRename.name"
                :disabled="busy"
                maxlength="50"
                :aria-label="`重命名${item.name}`"
                @keyup.enter="saveDictionaryRename(item)"
              />
            </template>
            <strong v-else>{{ item.name }}</strong>
            <small
              ><span class="status-pill" :class="{ enabled: item.is_active }">{{
                item.is_active ? "启用中" : "已停用"
              }}</span
              >{{ item.is_system ? "系统内置" : "我添加的" }}</small
            >
          </div>
          <div v-if="item.editable" class="dictionary-actions">
            <template v-if="dictionaryRename.id === item.id">
              <button class="text-button" :disabled="busy" @click="saveDictionaryRename(item)">
                保存
              </button>
              <button class="text-button" :disabled="busy" @click="cancelDictionaryRename(item)">
                取消
              </button>
            </template>
            <RowMenu
              v-else
              label="字典项操作"
              :items="dictionaryMenuItemsFor(item).map(action => ({...action, disabled:busy}))"
              @select="onDictionaryMenu(item, $event)"
            />
          </div>
          <small v-else class="me-dict-lock">系统内置，只读</small>
        </div>
        <p v-if="!dictionaryItems.length" class="empty-small">暂无字典项</p>
      </div>
    </div>

    <button class="outline me-logout" type="button" @click="logout">
      退出登录
    </button>
    <footer class="me-about-version" aria-label="网站版本信息">
      <span>关于往来有礼</span>
      <button type="button" class="version-link" @click="versionInfoOpen=true" aria-label="查看版本详情">
        版本与站点信息 <span aria-hidden="true">›</span>
      </button>
    </footer>
    <SiteAbout v-if="versionInfoOpen" :maintenance-available="maintenanceAvailable" @close="versionInfoOpen=false" />
  </section>
</template>

<style src="./me-page.css"></style>
