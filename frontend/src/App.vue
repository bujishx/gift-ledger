<script setup>
import {
  computed,
  nextTick,
  onMounted,
  onUnmounted,
  reactive,
  ref,
  watch,
} from "vue";
import { allPages, api, queryString, upload, sensitiveDownload } from "./api";
import { saveSensitiveDownload } from './lib/sensitiveDownload.js';
import { readMaintenanceAccess } from './lib/siteAccess.js';
import ThemeControl from './components/ThemeControl.vue';
import { createEventIdentityChecker, eventIdentityPayload, eventDisplayTitle } from './lib/eventIdentity.js';
import { createDetailAvailability, contactsForList } from './lib/detailAvailability.js';
import AuthPage from "./pages/AuthPage.vue";
import StatsPage from "./pages/StatsPage.vue";
import MePage from "./pages/MePage.vue";
import BusinessGuide from "./components/BusinessGuide.vue";
import ListWorkspace from "./components/ListWorkspace.vue";
import SearchToolbar from "./components/SearchToolbar.vue";
import SortSelect from "./components/SortSelect.vue";
import ModalShell from "./components/ModalShell.vue";
import DetailCard from "./components/DetailCard.vue";
import RelatedViewBanner from './components/RelatedViewBanner.vue';
import EntryEventPicker from './components/EntryEventPicker.vue';
import EntryEventSummary from './components/EntryEventSummary.vue';
import {usePageNavigation} from './composables/usePageNavigation.js';
import UiDialog from "./components/UiDialog.vue";
import { useDialog } from "./composables/useDialog";
import RowMenu from "./components/RowMenu.vue";
import UiIcon from "./components/UiIcon.vue";
import { money, signedMoney, balanceClass, stamp, eventDate, isCompleteEventDate, canCompleteEventDate } from "./lib/format.js";
import { canImport, setImportDecision } from './lib/legacyImport.js';
import AmountInput from "./components/AmountInput.vue";
import RemarkTextarea from "./components/RemarkTextarea.vue";
import ContactPicker from "./components/ContactPicker.vue";
import MultiSelect from "./components/MultiSelect.vue";
import { buildFilterChips, removeFilterChip } from "./lib/filterChips.js";
import { statisticsStorageKey, readStatisticsView, writeStatisticsView, clearStatisticsViews } from "./lib/statisticsSession.js";
import { useFloatingCard } from "./composables/useFloatingCard";
import {
  GIFT_SEARCH_SCOPE,
  eventContextParams,
  giftClass,
  giftMark,
  isReauthError,
  ledgerContextParams,
  queryContactLabel,
} from "./lib/ledgerView";

const tabs = [
  { id: "people", step: 1, label: "联系人" },
  { id: "events", step: 2, label: "事件" },
  { id: "ledger", step: 3, label: "记账" },
  { id: "stats", icon: "▥", label: "统计" },
  { id: "me", icon: "♙", label: "我的" },
];
const initialPage = new URLSearchParams(location.search).get("page");
const page = ref(tabs.some(tab => tab.id === initialPage) ? initialPage : "people");
const dialog = useDialog();
const formSnapshots = {};
const entryLoading = ref(false);
const entrySelecting = ref(false);
let noticeTimer;
const loading = ref(true);
const busy = ref(false);
const account = ref(null);
const statisticsView = ref(null);
let statisticsKey = '';
async function restoreStatisticsView() {
  const marker = document.cookie.match(/(?:^|;\s*)gift_ledger_csrf=([^;]+)/)?.[1] || '';
  statisticsKey = await statisticsStorageKey(account.value.id, marker);
  statisticsView.value = readStatisticsView(statisticsKey);
}
function saveStatisticsView(value) {
  statisticsView.value = value;
  writeStatisticsView(statisticsKey, value);
}
const notice = ref("");
const maintenanceAvailable = ref(false);
const error = ref("");
const authMode = ref("login");
const credentials = reactive({ username: "", password: "" });
const profileForm = reactive({ name: "" });
const profile = ref(null);
const contacts = ref([]);
const dictionaries = reactive({ relationship: [], kinship: [], event: [] });
const peopleBalances = ref([]);
const peopleSearch = ref("");
const appliedPeopleSearch = ref("");
const peopleLoading = ref(false);
const guideOpen = ref(false);
const peopleQueryError = ref(''), eventQueryError = ref(''), ledgerQueryError = ref('');
const eventsLoading = ref(false);
const ledgerLoading = ref(false);
let peopleSearchTimer, eventSearchTimer, ledgerSearchTimer;
let peopleRequest = 0, eventsRequest = 0, ledgerRequest = 0, eventDetailRequest = 0;
const peopleFilterOpen = ref(false);
const peopleContext = ref(null);
const peopleFilters = reactive({
  relations: [],
  statuses: [],
  flows: [],
  date_from: "",
  date_to: "",
});
const peoplePage = ref(1);
const peoplePageSize = ref(20);
const peopleSort = reactive({ field: "created_at", direction: "desc" });
const selectedPerson = ref(null);
const personCard = useFloatingCard();
const contactFormOpen = ref(false);
const contactEditingId = ref(null);
const contactForm = reactive({
  name: "",
  relationship_type_id: null,
  phone: "",
  remark: "",
});
const eventSearch = ref("");
const eventFilterOpen = ref(false);
const eventFilters = reactive({
  relations: [],
  kinships: [],
  types: [],
  date_from: "",
  date_to: "",
});
const events = ref([]);
const eventsTotal = ref(0);
const eventPage = ref(1);
const eventPageSize = ref(20);
const eventSort = reactive({ sort_by: "registered_at", sort_order: "desc" });
const selectedEvent = ref(null);
const eventCard = useFloatingCard();
const eventContext = ref(null);
const eventFormOpen = ref(false);
const eventEditingId = ref(null);
const eventEditingSource = ref(null);
const eventForm = reactive({
  host_contact_id: null,
  kinship_type_id: null,
  event_type_id: null,
  event_date: today(),
  remark: "",
});
const eventConflictExpanded = ref(false);
const eventDraftReturn = ref(null);
const entryConflictExpanded = ref(false);
const entryDraftReturn = ref(null);
const eventIdentityMatch = ref(null), eventIdentityChecking = ref(false), eventIdentityError = ref('');
let eventIdentityTimer;
const identityChecker = createEventIdentityChecker(body => api('/events/check-identity', { method:'POST', body }));
const duplicateEvent = computed(() => eventIdentityMatch.value);
const identitySignature = computed(() => JSON.stringify(eventIdentityPayload(eventForm, eventEditingId.value)));
function invalidateEventIdentity() {
  clearTimeout(eventIdentityTimer);
  identityChecker.invalidate();
  eventIdentityMatch.value = null;
  eventIdentityChecking.value = false;
  eventIdentityError.value = '';
  eventConflictExpanded.value = false;
}
async function checkEventIdentity() {
  clearTimeout(eventIdentityTimer);
  if (!eventFormOpen.value || !account.value) return null;
  const payload = eventIdentityPayload(eventForm, eventEditingId.value);
  const checkedAccount = account.value.id, checkedSignature = identitySignature.value;
  eventIdentityChecking.value = Boolean(payload);
  const result = await identityChecker.check(payload);
  if (!result.current || checkedAccount !== account.value?.id || checkedSignature !== identitySignature.value || !eventFormOpen.value) return { ...result, current:false };
  eventIdentityChecking.value = false;
  eventIdentityMatch.value = result.existingEvent || null;
  eventIdentityError.value = result.error ? '事件查重暂时失败，保存时将再次核对。' : '';
  return result;
}
watch([identitySignature, eventFormOpen, () => account.value?.id], () => {
  invalidateEventIdentity();
  if (eventFormOpen.value && account.value && eventIdentityPayload(eventForm, eventEditingId.value)) {
    eventIdentityChecking.value = true;
    eventIdentityTimer = setTimeout(checkEventIdentity, 250);
  }
});
const ledgerSearch = ref("");
const ledgerFilterOpen = ref(false);
const ledgerFilters = reactive({
  relations: [],
  kinships: [],
  types: [],
  date_from: "",
  date_to: "",
});
const ledger = ref({ items: [], page: { total: 0 }, totals: {} });
const ledgerPage = ref(1);
const ledgerPageSize = ref(20);
const ledgerSort = reactive({ sort_by: "gift_created_at", sort_order: "desc" });
const ledgerContext = ref(null);
const ledgerGift = ref(null);
const giftCard = useFloatingCard();
const detailAvailabilityStates = reactive({});
const detailAvailability = createDetailAvailability(api, detailAvailabilityStates);
const detailAvailabilityEpoch = ref(0);
let detailLinkRequest = 0;
function invalidateDetailAvailability() {
  detailAvailability.invalidate();
  detailAvailabilityEpoch.value++;
  detailLinkRequest++;
}
function detailAvailabilityState(kind, contact) {
  return detailAvailability.read(kind, contact?.id);
}
function detailLinkDisabled(kind, contact) {
  const state = detailAvailabilityState(kind, contact);
  return busy.value || state.status !== 'ready' || state.total === 0;
}
function detailLinkTitle(kind, contact) {
  const state = detailAvailabilityState(kind, contact);
  if (state.status === 'error') return '资料数量暂时检查失败，请重试';
  if (state.status !== 'ready') return '正在核对可查看的资料';
  if (state.total === 0) return kind === 'events' ? '暂无此人作为事主的事件' : kind === 'eventGifts' ? '此事件暂无记账' : '暂无与此人相关的记账';
  return '';
}
const visibleDetailChecks = computed(() => {
  if (page.value === 'people' && selectedPerson.value) return [
    { kind: 'events', contact: selectedPerson.value },
    { kind: 'gifts', contact: selectedPerson.value },
  ];
  if (page.value === 'ledger' && ledgerGift.value?.counterparty) return [
    { kind: 'events', contact: ledgerGift.value.counterparty },
  ];
  if (page.value === 'events' && selectedEvent.value) return [
    { kind: 'eventGifts', contact: selectedEvent.value.event },
  ];
  return [];
});
const detailChecksFailed = computed(() => visibleDetailChecks.value.some(check =>
  detailAvailabilityState(check.kind, check.contact).status === 'error'));
const detailChecksLoading = computed(() => visibleDetailChecks.value.some(check =>
  ['idle', 'loading'].includes(detailAvailabilityState(check.kind, check.contact).status)));
async function retryDetailAvailability() {
  await Promise.all(visibleDetailChecks.value.map(check =>
    detailAvailability.load(check.kind, check.contact.id, { refresh: true })));
}
watch([() => account.value?.id, page, () => selectedPerson.value?.id,
  () => ledgerGift.value?.counterparty?.id, () => selectedEvent.value?.event.id,
  detailAvailabilityEpoch], () => {
  detailLinkRequest++;
  if (!account.value?.profile_completed) return;
  for (const check of visibleDetailChecks.value) detailAvailability.load(check.kind, check.contact.id, { refresh: true });
});
watch(() => account.value?.id, invalidateDetailAvailability, { flush: 'sync' });
/** 点击入口时重新核对数量，旧账号或已关闭详情的慢请求不能触发跳转。 */
async function allowDetailLink(kind, id) {
  if (busy.value || !id || !account.value?.profile_completed) return false;
  const request = ++detailLinkRequest, checkedAccount = account.value.id;
  const result = await detailAvailability.load(kind, id, { refresh: true });
  if (!result.current || request !== detailLinkRequest || checkedAccount !== account.value?.id) return false;
  if (result.status === 'error') {
    popupError(new Error('暂时无法检查可查看的资料，请重试。'));
    return false;
  }
  return result.total > 0;
}
const floatingPhoneView = ref(window.innerWidth <= 700);
const floatingCardOpen = computed(() => Boolean(
  (selectedPerson.value && page.value === 'people') ||
  (selectedEvent.value && page.value === 'events') ||
  (ledgerGift.value && page.value === 'ledger')
));
const ledgerRowMenuId = ref(null);
const giftTip = ref({ text: "", left: 0, top: 0 });
const ledgerSearchHintOpen = ref(false);
const reauthOpen = ref(false);
const reauthPassword = ref("");
const reauthBusy = ref(false);
const reauthError = ref("");
let reauthResolve = null;
const sensitiveOpen = ref(false), sensitivePassword = ref(''), sensitiveBusy = ref(false), sensitiveError = ref('');
const sensitiveAction = ref(null);
let sensitiveResolve = null, sensitiveGeneration = 0;
function loginMarker() {
  return `${account.value?.id || ''}:${document.cookie.match(/(?:^|;\s*)gift_ledger_csrf=([^;]+)/)?.[1] || ''}`;
}
function closeSensitiveAuthorization(grant = null) {
  sensitiveGeneration++;
  const resolve = sensitiveResolve;
  sensitiveResolve = null; sensitiveOpen.value = false; sensitivePassword.value = ''; sensitiveError.value = ''; sensitiveAction.value = null; sensitiveBusy.value = false;
  resolve?.(grant);
}
function requestSensitiveAuthorization(purpose, context, label) {
  if (!account.value || sensitiveOpen.value) return Promise.resolve(null);
  const marker = loginMarker();
  sensitiveAction.value = { purpose, context, label, marker };
  sensitivePassword.value = ''; sensitiveError.value = ''; sensitiveOpen.value = true;
  return new Promise(resolve => { sensitiveResolve = resolve; });
}
async function submitSensitiveAuthorization() {
  if (sensitiveBusy.value || !sensitivePassword.value || !sensitiveAction.value) return;
  const generation = sensitiveGeneration, action = sensitiveAction.value;
  sensitiveBusy.value = true; sensitiveError.value = '';
  try {
    const result = await api('/auth/sensitive-actions/verify', { method: 'POST', body: { password: sensitivePassword.value, purpose: action.purpose, ...action.context } });
    if (generation !== sensitiveGeneration) return;
    if (action.marker !== loginMarker()) { closeSensitiveAuthorization(); return; }
    closeSensitiveAuthorization({ grant: result.grant, marker: action.marker });
  } catch (err) {
    if (generation === sensitiveGeneration) {
      if (action.marker !== loginMarker()) closeSensitiveAuthorization();
      else { sensitiveError.value = err.message; sensitivePassword.value = ''; }
    }
  } finally { if (generation === sensitiveGeneration) sensitiveBusy.value = false; }
}
async function verifiedDownload(purpose, context, label, path, filename) {
  if (busy.value) return;
  const authorization = await requestSensitiveAuthorization(purpose, context, label);
  if (!authorization || authorization.marker !== loginMarker()) return;
  busy.value = true;
  try {
    const file = await sensitiveDownload(path, authorization.grant, filename);
    if (authorization.marker !== loginMarker()) return;
    saveSensitiveDownload(file);
    success(`${label}已下载`);
  } catch (err) { if (authorization.marker === loginMarker()) popupError(err); }
  finally { busy.value = false; }
}
function exportAllData() { return verifiedDownload('export_all', {}, '导出全部数据', '/exports/all.xlsx', '往来有礼全部数据.xlsx'); }
function downloadPersonalBackup() { return verifiedDownload('personal_backup', {}, '下载个人账本备份', '/backups/personal', '往来有礼个人账本.giftledger'); }
function downloadSafetyBackup() {
  const token = safetyBackupToken.value;
  if (!token) return;
  return verifiedDownload('safety_backup', { safety_backup_token: token }, '下载恢复前安全备份', `/restores/personal/safety-backups/${encodeURIComponent(token)}`, '往来有礼恢复前安全备份.giftledger');
}
const entryOpen = ref(false);
const entryEventSearch = ref("");
const entryEventOptions = ref([]);
const entryEvent = ref(null);
const entryCompleteDate = ref('');
const entryCards = ref([blankCard()]);
const giftEditOpen = ref(false);
const giftEditDetail = ref(null);
const giftEditEvent = ref(null);
const giftEditForm = reactive({
  giver_contact_id: null,
  amount: "",
  gift_details: "",
  remark: "",
});
const meSection = ref("data");
const dictionaryCategory = ref("relationship");
const dictionaryDraft = ref("");
const dictionaryRename = reactive({ id: null, name: "" });
const importFile = ref(null);
const importPreview = ref(null);
const importDecisions = ref({});
const importDecisionsDirty = ref(false);
const importAcknowledged = ref(false);
const importResult = ref(null);
const importResultUncertain = ref(false);
const importRecoveryToken = ref('');
const importRecoveryUnavailable = ref(false);
const importBatches = ref([]);
const importBatchLoading = ref(false);
const importBatchError = ref('');
const restoreFile = ref(null);
const restorePreview = ref(null);
const restorePassword = ref("");
const restoreAcknowledged = ref(false);
const restoreRenames = ref([]);
const safetyBackupToken = ref(
  sessionStorage.getItem("gift_ledger_safety_token") || "",
);

function today() {
  return new Date().toLocaleDateString("en-CA");
}
function blankCard() {
  return { giver_contact_id: null, amount: "", gift_details: "", remark: "" };
}
function summaryName(contact) {
  return [contact?.name, contact?.relationship_type_name, contact?.remark]
    .filter(Boolean)
    .join(" · ");
}
function identityPart(value) {
  return (value || "")
    .trim()
    .replace(/[A-Z]/g, (letter) => letter.toLowerCase());
}
const sameNameContacts = computed(() =>
  contacts.value.filter(
    (contact) =>
      contact.id !== contactEditingId.value &&
      identityPart(contact.name) === identityPart(contactForm.name) &&
      identityPart(contactForm.name) !== "",
  ),
);
const duplicateContact = computed(() =>
  sameNameContacts.value.find(
    (contact) =>
      identityPart(contact.remark) === identityPart(contactForm.remark),
  ),
);
function eventTitle(row) { return eventDisplayTitle(row, contacts.value); }
function localContact(id) {
  return contacts.value.find((x) => x.id === id) || null;
}
/** 事主：姓名(关系类型·联系人备注)，本人只显示姓名。 */
function hostLabel(row) {
  const host = row?.event?.host;
  if (!host) return "—";
  const local = localContact(host.id);
  if (!local || local.is_self) return host.name;
  const detail = [local.relationship_type_name, local.remark]
    .filter(Boolean)
    .join("·");
  return detail ? `${host.name}(${detail})` : host.name;
}
function popupError(err) {
  error.value = err.message || "操作失败，请重试";
  notice.value = "";
  nextTick(() => {
    const alerts = [...document.querySelectorAll('.modal-body .alert.error, .me-feedback .alert.error')];
    alerts.at(-1)?.scrollIntoView({block:'nearest'});
  });
}
function success(message) {
  clearTimeout(noticeTimer);
  notice.value = message;
  noticeTimer = setTimeout(()=> { notice.value = ""; },6000);
  error.value = "";
}
function options(category) {
  return dictionaries[category].map((x) => ({ value: x.id, label: x.name }));
}
const relationshipOptions = computed(() => options("relationship"));
const kinshipOptions = computed(() => options("kinship"));
const eventTypeOptions = computed(() => options("event"));
const peopleFilterChips = computed(() => buildFilterChips(peopleFilters, [
  {key:'relations',label:'关系',options:relationshipOptions.value},
  {key:'statuses',label:'状态',options:[{value:'active',label:'启用中'},{value:'inactive',label:'已停用'}]},
  {key:'flows',label:'来往状态',options:[{value:'self_unreturned',label:'我未回礼'},{value:'counterparty_unreturned',label:'对方未回礼'},{value:'mutual',label:'礼尚往来'},{value:'none',label:'暂无往来'}]},
], '添加日期'));
const businessFilterDefinitions = computed(() => [
  {key:'relations',label:'事主关系',options:relationshipOptions.value},
  {key:'kinships',label:'亲属',options:kinshipOptions.value},
  {key:'types',label:'事件类型',options:eventTypeOptions.value},
]);
const eventFilterChips = computed(() => buildFilterChips(eventFilters, businessFilterDefinitions.value, '办事日期'));
const ledgerFilterChips = computed(() => buildFilterChips(ledgerFilters, businessFilterDefinitions.value, '办事日期'));
const selfContact = computed(() => contacts.value.find((x) => x.is_self));
const balanceById = computed(
  () => new Map(peopleBalances.value.map((x) => [x.contact.id, x])),
);
function personBalance(id) {
  return balanceById.value.get(id) || null;
}
const filteredPeople = computed(() => {
  const words = appliedPeopleSearch.value.trim().split(/\s+/).filter(Boolean);
  return contactsForList(contacts.value).filter((x) => {
    if (peopleContext.value && x.id !== peopleContext.value.id) return false;
    if (
      words.length &&
      !words.some((w) => `${x.name} ${x.remark || ""}`.includes(w))
    )
      return false;
    if (
      peopleFilters.relations.length &&
      !peopleFilters.relations.includes(x.relationship_type_id)
    )
      return false;
    if (
      peopleFilters.statuses.length &&
      !peopleFilters.statuses.includes(x.is_active ? "active" : "inactive")
    )
      return false;
    const flow =
      balanceById.value.get(x.id)?.interaction_status?.code || "none";
    if (peopleFilters.flows.length && !peopleFilters.flows.includes(flow))
      return false;
    const created = x.created_at?.slice(0, 10) || "";
    if (peopleFilters.date_from && created < peopleFilters.date_from)
      return false;
    if (peopleFilters.date_to && created > peopleFilters.date_to) return false;
    return true;
  });
});
function peopleSortValue(person) {
  const balance = balanceById.value.get(person.id);
  switch (peopleSort.field) {
    case "name":
      return person.name;
    case "relationship":
      return person.relationship_type_name;
    case "remark":
      return person.remark || "";
    case "flow":
      return balance?.interaction_status?.label || "暂无往来";
    case "expense":
      return Number(balance?.expense_amount || 0);
    case "income":
      return Number(balance?.income_amount || 0);
    case "balance":
      return Number(balance?.balance?.amount || 0);
    default:
      return person.created_at || "";
  }
}
const sortedPeople = computed(() =>
  [...filteredPeople.value].sort((a, b) => {
    const av = peopleSortValue(a);
    const bv = peopleSortValue(b);
    const comparison =
      typeof av === "number" && typeof bv === "number"
        ? av - bv
        : String(av).localeCompare(String(bv), "zh-CN", { numeric: true });
    return (
      (peopleSort.direction === "asc" ? comparison : -comparison) || b.id - a.id
    );
  }),
);
const visiblePeople = computed(() =>
  sortedPeople.value.slice(
    (peoplePage.value - 1) * peoplePageSize.value,
    peoplePage.value * peoplePageSize.value,
  ),
);
const peoplePages = computed(() =>
  Math.max(1, Math.ceil(filteredPeople.value.length / peoplePageSize.value)),
);
const peoplePageTokens = computed(() => {
  if (peoplePages.value <= 3)
    return Array.from({ length: peoplePages.value }, (_, i) => i + 1);
  const pages = [...new Set([1, peoplePage.value, peoplePages.value])].sort(
    (a, b) => a - b,
  );
  const result = [];
  for (let i = 0; i < pages.length; i += 1) {
    if (i && pages[i] - pages[i - 1] > 1)
      result.push(`gap-${pages[i] - pages[i - 1] - 1}`);
    result.push(pages[i]);
  }
  return result;
});
function peopleGapText(token) {
  return "·".repeat(Math.min(3, Number(token.slice(4))));
}
function sortPeople(field) {
  if (peopleSort.field === field)
    peopleSort.direction = peopleSort.direction === "asc" ? "desc" : "asc";
  else {
    peopleSort.field = field;
    peopleSort.direction = field === "created_at" ? "desc" : "asc";
  }
  peoplePage.value = 1;
}
function peopleSortArrow(field) {
  return peopleSort.field === field
    ? peopleSort.direction === "asc"
      ? "▲"
      : "▼"
    : "↕";
}
watch(
  [peopleSearch, peopleFilters],
  () => {
    if (restoringPage.value) return;
    peoplePage.value = 1;
  },
  { deep: true },
);
watch(peoplePageSize, (size) => {
  if (restoringPage.value) return;
  peoplePage.value = 1;
  if (account.value)
    localStorage.setItem(
      `gift_ledger_people_page_size_${account.value.id}`,
      String(size),
    );
});
watch(peoplePages, (count) => {
  if (restoringPage.value) return;
  if (peoplePage.value > count) peoplePage.value = count;
});
const entryIsSelf = computed(
  () => entryEvent.value?.event.host.id === selfContact.value?.id,
);
const entryExternalAlreadyRecorded = computed(
  () =>
    !!entryEvent.value &&
    !entryIsSelf.value &&
    entryEvent.value.gifts.some((g) => g.giver.id === selfContact.value?.id),
);
const entryConflictFromServer = ref(null);
const entryDuplicateGift = computed(() => entryConflictFromServer.value || (entryEvent.value?.gifts || []).find(gift =>
  entryCards.value.some(card => card.giver_contact_id === gift.giver.id)) || null);
watch(entryDuplicateGift, () => { entryConflictExpanded.value = false; });
watch(() => entryCards.value.map(card => card.giver_contact_id).join(','), () => {
  if (entryConflictFromServer.value && !entryCards.value.some(card => card.giver_contact_id === entryConflictFromServer.value.giver.id)) entryConflictFromServer.value = null;
});
const entryAvailable = computed(() => {
  if (!entryEvent.value) return [];
  const existing = entryEvent.value.gifts.map((g) => g.giver.id);
  const selected = entryCards.value.map(card => card.giver_contact_id);
  return contacts.value.filter(
    (x) =>
      (x.is_active || selected.includes(x.id)) &&
      x.id !== entryEvent.value.event.host.id &&
      (!existing.includes(x.id) || selected.includes(x.id)),
  );
});
const entryEventMatches = computed(() => {
  const words = entryEventSearch.value.trim().split(/\s+/).filter(Boolean);
  return entryEventOptions.value.filter(
    (row) =>
      !words.length ||
      words.some((w) =>
        `${eventTitle(row)} ${row.event.remark || ""}`.includes(w),
      ),
  );
});
const giftEditIsSelf = computed(
  () => giftEditDetail.value?.event.host.id === selfContact.value?.id,
);
const giftEditCandidates = computed(() => {
  if (!giftEditDetail.value || !giftEditEvent.value) return [];
  const usedByOthers = giftEditEvent.value.gifts
    .filter((gift) => gift.id !== giftEditDetail.value.gift.id)
    .map((gift) => gift.giver.id);
  return contacts.value.filter(
    (contact) =>
      (contact.is_active || contact.id === giftEditForm.giver_contact_id) &&
      contact.id !== giftEditDetail.value.event.host.id &&
      !usedByOthers.includes(contact.id),
  );
});

async function bootstrap() {
  loading.value = true;
  try {
    account.value = await api("/auth/me");
    await restoreStatisticsView();
    if (account.value.profile_completed) await loadCore();
    if (sessionStorage.getItem('gift_ledger_import_result_user') === String(account.value?.id)) {
      importRecoveryToken.value = sessionStorage.getItem('gift_ledger_import_result_token') || '';
      importResultUncertain.value = Boolean(importRecoveryToken.value);
    } else clearImportRecovery();
  } catch (err) {
    if (err.status !== 401) popupError(err);
  } finally {
    loading.value = false;
    await nextTick();
    if (account.value?.profile_completed) pageNavigation.init(statisticsKey);
  }
}
async function authSubmit() {
  if (busy.value) return;
  busy.value = true;
  error.value = "";
  try {
    if (authMode.value === "register") {
      await api("/auth/register", { method: "POST", body: credentials });
      authMode.value = "login";
      success("注册成功，请登录并完善本人信息");
    } else {
      account.value = await api("/auth/login", {
        method: "POST",
        body: credentials,
      });
      resetPageState();
      credentials.password = "";
      clearStatisticsViews();
      await restoreStatisticsView();
      page.value = "people";
      if (account.value.profile_completed) {
        await loadCore();
        await nextTick();
        pageNavigation.init(statisticsKey);
      }
    }
  } catch (err) {
    popupError(err);
  } finally {
    busy.value = false;
  }
}
async function saveProfile() {
  if (busy.value) return;
  busy.value = true;
  try {
    profile.value = await api("/me/profile", {
      method: "POST",
      body: { name: profileForm.name.trim() },
    });
    account.value = await api("/auth/me");
    await loadCore();
    await nextTick();
    pageNavigation.init(statisticsKey);
    success("本人信息已建立");
  } catch (err) {
    popupError(err);
  } finally {
    busy.value = false;
  }
}
async function logout() {
  try {
    await api("/auth/logout", { method: "POST" });
    clearStatisticsViews();
    statisticsView.value = null;
    statisticsKey = '';
    account.value = null;
    pageNavigation.reset();
    resetPageState();
    profile.value = null;
    contacts.value = [];
    peopleContext.value = null;
    ledgerContext.value = null;
    eventContext.value = null;
    ledgerGift.value = null;
    selectedEvent.value = null;
    selectedPerson.value = null;
    if (reauthOpen.value) closeReauth(false);
    safetyBackupToken.value = "";
    sessionStorage.removeItem("gift_ledger_safety_token");
    sessionStorage.removeItem("gift_ledger_safety_user_id");
  } catch (err) {
    popupError(err);
  }
}
// 仅在登录边界清空内存资料；同一登录内普通切页仍保留草稿与查询。
function resetPageState() {
  closeSensitiveAuthorization();
  invalidateEventIdentity();
  invalidateDetailAvailability();
  eventDraftReturn.value = entryDraftReturn.value = null;
  entryConflictFromServer.value = null;
  entryConflictExpanded.value = false;
  peopleRequest++; eventsRequest++; ledgerRequest++; eventDetailRequest++;
  clearTimeout(peopleSearchTimer); clearTimeout(eventSearchTimer); clearTimeout(ledgerSearchTimer);
  contacts.value = []; peopleBalances.value = []; events.value = []; eventsTotal.value = 0;
  ledger.value = { items: [], page: { total: 0 }, totals: {} };
  Object.assign(dictionaries, { relationship: [], kinship: [], event: [] });
  peopleContext.value = null; eventContext.value = null; ledgerContext.value = null;
  peopleSearch.value = ''; appliedPeopleSearch.value = ''; eventSearch.value = ''; ledgerSearch.value = '';
  Object.assign(peopleFilters, { relations: [], statuses: [], flows: [], date_from: '', date_to: '' });
  for (const filters of [eventFilters, ledgerFilters]) Object.assign(filters, { relations: [], kinships: [], types: [], date_from: '', date_to: '' });
  peoplePage.value = eventPage.value = ledgerPage.value = 1;
  peoplePageSize.value = eventPageSize.value = ledgerPageSize.value = 20;
  Object.assign(peopleSort, { field: 'created_at', direction: 'desc' });
  Object.assign(eventSort, { sort_by: 'registered_at', sort_order: 'desc' });
  Object.assign(ledgerSort, { sort_by: 'gift_created_at', sort_order: 'desc' });
  peopleFilterOpen.value = eventFilterOpen.value = ledgerFilterOpen.value = guideOpen.value = false;
  peopleQueryError.value = eventQueryError.value = ledgerQueryError.value = '';
  peopleLoading.value = eventsLoading.value = ledgerLoading.value = false;
  closeFloatingCards();
  for (const flag of Object.values(formOpenFlags)) flag.value = false;
  entryEvent.value = null; entryEventOptions.value = []; entryEventSearch.value = ''; entryCards.value = [blankCard()];
  giftEditDetail.value = giftEditEvent.value = null;
  Object.assign(contactForm, { name: '', relationship_type_id: null, phone: '', remark: '' });
  Object.assign(eventForm, { host_contact_id: null, kinship_type_id: null, event_type_id: null, event_date: today(), remark: '' });
  Object.assign(giftEditForm, { giver_contact_id: null, amount: '', gift_details: '', remark: '' });
  contactEditingId.value = eventEditingId.value = null;
  meSection.value = 'data'; dictionaryCategory.value = 'relationship'; dictionaryDraft.value = '';
  Object.assign(dictionaryRename, { id: null, name: '' });
  importFile.value = importPreview.value = restoreFile.value = restorePreview.value = null;
  importDecisions.value = {}; importDecisionsDirty.value = false; importAcknowledged.value = false;
  importResult.value = null; importResultUncertain.value = false; importBatches.value = [];
  importBatchLoading.value = false; importBatchError.value = ''; eventEditingSource.value = null; entryCompleteDate.value = '';
  clearImportRecovery();
  restorePassword.value = ''; restoreAcknowledged.value = false; restoreRenames.value = [];
  profileForm.name = ''; credentials.password = '';
  if (reauthOpen.value) closeReauth(false);
  reauthPassword.value = ''; reauthError.value = '';
}
async function loadCore() {
  if (
    sessionStorage.getItem("gift_ledger_safety_user_id") !==
    String(account.value.id)
  ) {
    safetyBackupToken.value = "";
    sessionStorage.removeItem("gift_ledger_safety_token");
    sessionStorage.removeItem("gift_ledger_safety_user_id");
  }
  const savedPageSize = Number(
    localStorage.getItem(`gift_ledger_people_page_size_${account.value.id}`),
  );
  peoplePageSize.value = [20, 50, 100].includes(savedPageSize)
    ? savedPageSize
    : 20;
  const [contactRows, relation, kinship, event, myProfile] = await Promise.all([
    allPages("/contacts", { include_inactive: true }),
    api("/dictionaries/relationship?include_inactive=true"),
    api("/dictionaries/kinship?include_inactive=true"),
    api("/dictionaries/event?include_inactive=true"),
    api("/me/profile"),
  ]);
  contacts.value = contactRows.sort(
    (a, b) =>
      (b.created_at || "").localeCompare(a.created_at || "") || b.id - a.id,
  );
  dictionaries.relationship = relation.items;
  dictionaries.kinship = kinship.items;
  dictionaries.event = event.items;
  profile.value = myProfile;
  await loadPage();
}
async function loadPage({ throwOnError = false } = {}) {
  if (!account.value?.profile_completed) return;
  try {
    if (page.value === "people") await loadPeople();
    if (page.value === "events") await loadEvents(throwOnError);
    if (page.value === "ledger") await loadLedger(throwOnError);

    if (page.value === "me") return;
  } catch (err) {
    if (throwOnError) throw err;
    popupError(err);
  }
}
const pageRouteKey=ref(0);
const pageNavigation=usePageNavigation({
 page,
 states:{
  people:{read:()=>({search:peopleSearch.value,applied:appliedPeopleSearch.value,filters:peopleFilters,sort:peopleSort,page:peoplePage.value,size:peoplePageSize.value,open:false,context:peopleContext.value}),write:v=>{peopleSearch.value=v.search;appliedPeopleSearch.value=v.applied;Object.assign(peopleFilters,v.filters);Object.assign(peopleSort,v.sort);peoplePage.value=v.page;peoplePageSize.value=v.size;peopleFilterOpen.value=false;peopleContext.value=v.context||null;}},
  events:{read:()=>({search:eventSearch.value,filters:eventFilters,sort:eventSort,page:eventPage.value,size:eventPageSize.value,open:false,context:eventContext.value}),write:v=>{eventSearch.value=v.search;Object.assign(eventFilters,v.filters);Object.assign(eventSort,v.sort);eventPage.value=v.page;eventPageSize.value=v.size;eventFilterOpen.value=false;eventContext.value=v.context;}},
  ledger:{read:()=>({search:ledgerSearch.value,filters:ledgerFilters,sort:ledgerSort,page:ledgerPage.value,size:ledgerPageSize.value,open:false,context:ledgerContext.value}),write:v=>{ledgerSearch.value=v.search;Object.assign(ledgerFilters,v.filters);Object.assign(ledgerSort,v.sort);ledgerPage.value=v.page;ledgerPageSize.value=v.size;ledgerFilterOpen.value=false;ledgerContext.value=v.context;}},
  stats:{read:()=>statisticsView.value,write:saveStatisticsView},
  me:{read:()=>({section:meSection.value,category:dictionaryCategory.value}),write:v=>{meSection.value=v.section;dictionaryCategory.value=v.category;}},
 },
 load:async()=>{notice.value='';error.value='';await loadPage({throwOnError:true});},closeDetails:closeFloatingCards,
 readDetails:()=>selectedPerson.value?{kind:'people',id:selectedPerson.value.id}:selectedEvent.value?{kind:'events',id:selectedEvent.value.event.id}:ledgerGift.value?{kind:'ledger',id:ledgerGift.value.id}:null,
 restoreDetails:restorePageDetail,
 routeChanged:()=>pageRouteKey.value++,
 confirmLeave:async()=>{if(busy.value || sensitiveOpen.value || entrySelecting.value)return false;for(const [kind,flag] of Object.entries(formOpenFlags)){if(flag.value && !await closeForm(kind))return false;}return true;},
 onError:popupError,
});
const relatedView=pageNavigation.related,restoringPage=pageNavigation.restoring;
const relatedCount=computed(()=>page.value==='people'?sortedPeople.value.length:page.value==='events'?eventsTotal.value:ledger.value.page.total);
const relatedUnit=computed(()=>page.value==='people'?'人':page.value==='events'?'个事件':'笔');
const relatedConditions=computed(()=>{
 const c=ledgerContext.value;if(page.value!=='ledger'||c?.kind!=='statistics')return [];
 const p=c.params;
 const contactName=id=>queryContactLabel(localContact(Number(id)));
 const dictName=(kind,id)=>dictionaries[kind].find(x=>x.id===Number(id))?.name || '#'+id;
 return [
  p.direction&&{key:'direction',label:'收支：'+(p.direction==='income'?'我收到':'我随出')},
  p.relationship_type_id&&{key:'relationship_type_id',label:'相关人关系：'+dictName('relationship',p.relationship_type_id)},
  p.contact_id&&{key:'contact_id',label:'关联联系人：'+contactName(p.contact_id)},
  p.event_type_id&&{key:'event_type_id',label:'事件类型：'+dictName('event',p.event_type_id)},
  p.event_id&&{key:'event_id',label:'具体事件：'+(c.eventLabel || '#'+p.event_id)},
  p.interaction_status&&{key:'interaction_status',label:'历史来往状态：'+({self_unreturned:'我未回礼',counterparty_unreturned:'对方未回礼',mutual:'礼尚往来',none:'暂无往来'}[p.interaction_status]||p.interaction_status)},
  ...(p.counterparty_relationship_type_ids || []).map(id=>({key:`counterparty_relationship_type_ids:${id}`,label:'相关人关系：'+dictName('relationship',id)})),
  ...(p.statistics_event_type_ids || []).map(id=>({key:`statistics_event_type_ids:${id}`,label:'统计事件类型：'+dictName('event',id)})),
  ...(p.interaction_statuses || []).map(value=>({key:`interaction_statuses:${value}`,label:'历史来往状态：'+({self_unreturned:'我未回礼',counterparty_unreturned:'对方未回礼',mutual:'礼尚往来',none:'暂无往来'}[value]||value)})),
  p.date_basis==='registered'&&{key:'date_basis',label:'日期口径：登记日期'},
 ].filter(Boolean);
});
async function removeRelatedCondition(key){
  if(ledgerContext.value?.kind!=='statistics')return;
  const [group,value] = key.split(':');
  if (value && Array.isArray(ledgerContext.value.params[group])) {
    ledgerContext.value.params[group] = ledgerContext.value.params[group].filter(item => String(item) !== value);
    if (!ledgerContext.value.params[group].length) delete ledgerContext.value.params[group];
  } else delete ledgerContext.value.params[key];
  ledgerPage.value=1;await loadLedger();
}
async function restorePageDetail(detail){
 await nextTick();
 const phone=window.innerWidth<=700;
 if(detail.kind==='people'){const person=localContact(detail.id);if(!person)return;const row=document.querySelector((phone?'.mobile-list':'.people-table')+` [data-person-id="${detail.id}"]`);selectedPerson.value=person;await personCard.place(row?.getBoundingClientRect()||null);}
 if(detail.kind==='events'){
   const request=++eventDetailRequest;
   const result=await api(`/events/${detail.id}`);
   if(request!==eventDetailRequest || page.value!=='events' || !account.value?.profile_completed)return;
   selectedEvent.value=result;
   const row=events.value.findIndex(x=>x.event.id===detail.id);
   const el=document.querySelectorAll(phone?'.event-mobile-card':'.events-table tbody tr')[Math.max(0,row)];
   await eventCard.place(el?.getBoundingClientRect()||null);
 }
 if(detail.kind==='ledger'){const index=ledger.value.items.findIndex(x=>x.id===detail.id);const gift=ledger.value.items[index];if(!gift)return;ledgerGift.value=gift;const el=document.querySelectorAll(phone?'.ledger-mobile-card':'.ledger-table tbody tr')[index];await giftCard.place(el?.getBoundingClientRect()||null);}
}
async function navigate(destination){guideOpen.value=false;return await pageNavigation.navigate(tabs.some(tab=>tab.id===destination)?destination:'people');}
async function guideAction(destination) {
  guideOpen.value = false;
  if (!await navigate(destination)) return;
  if (destination === 'people') openContact();
  else if (destination === 'events') openEventForm();
  else if (destination === 'ledger') await openEntry();
}
async function loadPeople() {
  const request = ++peopleRequest;
  peopleLoading.value = true; peopleQueryError.value='';
  try {
  const [rows, balances] = await Promise.all([
    allPages("/contacts", { include_inactive: true }),
    allPages("/ledger/query/people"),
  ]);
  if (request !== peopleRequest) return;
  invalidateDetailAvailability();
  contacts.value = rows.sort(
    (a, b) =>
      (b.created_at || "").localeCompare(a.created_at || "") || b.id - a.id,
  );
  peopleBalances.value = balances;
  if (peoplePage.value > peoplePages.value) peoplePage.value = peoplePages.value;
  } catch(err) { if(request === peopleRequest)peopleQueryError.value=err.message || '联系人加载失败，请重试';throw err; }
  finally { if (request === peopleRequest) peopleLoading.value = false; }
}
function closeFloatingCards() {
  detailLinkRequest++;
  eventDetailRequest++;
  selectedPerson.value = null;
  selectedEvent.value = null;
  ledgerGift.value = null;
  ledgerRowMenuId.value = null;
}
function dismissFloatingCards() {
  const card = selectedPerson.value ? personCard : selectedEvent.value ? eventCard : ledgerGift.value ? giftCard : null;
  closeFloatingCards();
  card?.restoreFocus();
}
async function togglePerson(person, event) {
  if (selectedPerson.value?.id === person.id) {
    selectedPerson.value = null;
    return;
  }
  selectedPerson.value = person;
  personCard.position.left = 12;
  personCard.position.top = 12;
  await personCard.place(personCard.anchorFrom(event));
}
/** 查看人员汇总：精确限定联系人 ID，并打开其详情。 */
async function openPersonCard(contactId) {
  if (!contactId) return;
  const label=queryContactLabel(localContact(contactId));
  await pageNavigation.associate('people',{title:'仅看联系人：'+label},()=>{
    peopleSearch.value='';appliedPeopleSearch.value='';
    peopleContext.value={id:Number(contactId),label};
    Object.assign(peopleFilters,{relations:[],statuses:[],flows:[],date_from:'',date_to:''});
    peopleSort.field='created_at';peopleSort.direction='desc';peoplePage.value=1;
  },async()=>{
    const person=localContact(contactId);
    if(!person){popupError(new Error('没有找到这位联系人'));return;}
    const index=sortedPeople.value.findIndex(x=>x.id===person.id);
    if(index>=0)peoplePage.value=Math.floor(index/peoplePageSize.value)+1;
    await nextTick();
    const row=document.querySelector((window.innerWidth<=700?'.mobile-list':'.people-table')+` [data-person-id="${person.id}"]`);
    row?.scrollIntoView({block:'center'});selectedPerson.value=person;
    await personCard.place(row?.getBoundingClientRect()||null);
  });
}
function keepPersonCardInView() {
  floatingPhoneView.value = window.innerWidth <= 700;
  personCard.keepInView();
  eventCard.keepInView();
  giftCard.keepInView();
}
function openContact(contact = null) {
  error.value = "";
  if (contact?.is_self) { meSection.value = 'data'; navigate('me'); return; }
  selectedPerson.value = null;
  contactEditingId.value = contact?.id || null;
  Object.assign(contactForm, {
    name: contact?.name || "",
    relationship_type_id: contact?.relationship_type_id || null,
    phone: contact?.phone || "",
    remark: contact?.remark || "",
  });
  contactFormOpen.value = true;
}
async function openEventForPerson(person) {
  if (busy.value || !person?.is_active) return;
  const id = person.id;
  if (await navigate("events")) openEventForm(null, id);
}
async function saveContact() {
  if (busy.value) return;
  if (duplicateContact.value) {
    error.value = "已有姓名和备注相同的联系人，请修改备注以便区分";
    return;
  }
  busy.value = true;
  try {
    const body = {
      name: contactForm.name.trim(),
      relationship_type_id: Number(contactForm.relationship_type_id),
      phone: contactForm.phone.trim() || null,
      remark: contactForm.remark.trim() || null,
    };
    if (contactEditingId.value)
      await api(`/contacts/${contactEditingId.value}`, {
        method: "PATCH",
        body,
      });
    else await api("/contacts", { method: "POST", body });
    contactFormOpen.value = false;
    selectedPerson.value = null;
    await loadPeople();
    success("联系人已保存");
  } catch (err) {
    popupError(err);
  } finally {
    busy.value = false;
  }
}
async function setContactActive(contact) {
  if (busy.value) return;
  if(contact.is_active && !(await dialog.ask({title:"停用联系人",message:`停用“${contact.name}”后，历史账目仍保留，新事件及记账记录不再待选。`,confirmLabel:"停用"}))) return;
  busy.value = true;
  try {
    await api(
      `/contacts/${contact.id}/${contact.is_active ? "deactivate" : "activate"}`,
      { method: "POST" },
    );
    await loadPeople();
    selectedPerson.value = null;
    success(contact.is_active ? "联系人已停用" : "联系人已启用");
  } catch (err) {
    popupError(err);
  } finally { busy.value = false; }
}
async function deleteContact(contact) {
  if (busy.value) return;
  if (!(await dialog.ask({title:"删除联系人",message:`确认删除“${contact.name}”？有引用的联系人不能删除。`,confirmLabel:"删除",confirmTone:'danger'}))) return;
  busy.value = true;
  try {
    await api(`/contacts/${contact.id}`, { method: "DELETE" });
    selectedPerson.value = null;
    await loadPeople();
    success("联系人已删除");
  } catch (err) {
    popupError(err);
  } finally { busy.value = false; }
}
function eventParams() {
  return {
    keyword: eventSearch.value.trim(),
    ...eventContextParams(eventContext.value),
    relationship_type_ids: eventFilters.relations,
    kinship_type_ids: eventFilters.kinships,
    event_type_ids: eventFilters.types,
    date_from: eventFilters.date_from,
    date_to: eventFilters.date_to,
    sort_by: eventSort.sort_by,
    sort_order: eventSort.sort_order,
    limit: eventPageSize.value,
    offset: (eventPage.value - 1) * eventPageSize.value,
  };
}
async function loadEvents(throwOnError = false) {
  clearTimeout(eventSearchTimer);
  const request = ++eventsRequest;
  if (eventFilters.date_from && eventFilters.date_to && eventFilters.date_from > eventFilters.date_to) { eventsLoading.value = false; popupError(new Error('开始日期不能晚于结束日期')); return; }
  eventsLoading.value = true; eventQueryError.value='';
  try {
    const result = await api('/events' + queryString(eventParams()));
    if (request !== eventsRequest) return;
    invalidateDetailAvailability();
    events.value = result.items;
    eventsTotal.value = result.total;
    const maxPage=Math.max(1,Math.ceil(result.total/eventPageSize.value));
    if(eventPage.value>maxPage){eventPage.value=maxPage;return await loadEvents(throwOnError);}
  } catch(err) { if(request === eventsRequest) { eventQueryError.value=err.message || '事件加载失败，请重试';if(throwOnError)throw err;popupError(err); } }
  finally { if(request === eventsRequest) eventsLoading.value = false; }
}
async function searchEvents() {
  eventPage.value = 1;
  await loadEvents();
}
/** 从联系人卡或记账记录卡跳转：按事主精确筛选事件列表。 */
async function openEventsForContact(contact) {
  if(!contact?.id || !await allowDetailLink('events', contact.id))return;
  const label=queryContactLabel(contact);
  await pageNavigation.associate('events',{title:'仅看事主为 '+label+' 的事件'},()=>{
    eventSearch.value='';Object.assign(eventFilters,{relations:[],kinships:[],types:[],date_from:'',date_to:''});
    eventContext.value={kind:'host',id:contact.id,label};eventPage.value=1;
  });
}
async function toggleEvent(row, event) {
  if (selectedEvent.value?.event.id === row.event.id) {
    closeEventCard();
    return;
  }
  const request = ++eventDetailRequest;
  const anchor = eventCard.anchorFrom(event);
  try {
    const detail = await api(`/events/${row.event.id}`);
    if (request !== eventDetailRequest || page.value !== 'events') return;
    selectedEvent.value = detail;
    eventCard.position.left = 12;
    eventCard.position.top = 12;
    await eventCard.place(anchor);
  } catch (err) {
    if (request === eventDetailRequest) popupError(err);
  }
}
function closeEventCard() {
  eventDetailRequest++;
  selectedEvent.value = null;
}
/** 事主是否就是“我”。 */
function eventHostIsSelf(row) {
  return row?.event?.host?.id === selfContact.value?.id;
}
/** 别人办事且我已经随过礼时，不允许重复登记。 */
function eventHasMyGift(row) {
  if (!row?.event || eventHostIsSelf(row)) return false;
  const selfId = selfContact.value?.id;
  return (row.gifts || []).some((gift) => gift.giver.id === selfId);
}
function openEventForm(row = null, hostId = null, preserveReturn = false) {
  error.value = "";
  closeFloatingCards();
  if (!preserveReturn) eventDraftReturn.value = null;
  eventConflictExpanded.value = false;
  invalidateEventIdentity();
  const event = row?.event || row;
  eventEditingId.value = event?.id || null;
  eventEditingSource.value = event || null;
  Object.assign(eventForm, {
    host_contact_id: hostId || event?.host?.id || null,
    kinship_type_id: event?.kinship_type?.id || null,
    event_type_id: event?.event_type?.id || null,
    event_date: event ? (isCompleteEventDate(event) ? event.event_date : '') : today(),
    remark: event?.remark || "",
  });
  eventFormOpen.value = true;
}
async function editDuplicateEvent() {
  const match = duplicateEvent.value;
  if (!match || busy.value) return;
  if (!eventDraftReturn.value) eventDraftReturn.value = {
    editingId: eventEditingId.value, source: eventEditingSource.value, values: { ...eventForm }, snapshot: formSnapshots.event,
  };
  openEventForm(match, null, true);
  await nextTick();
  formSnapshots.event = formValue('event');
}
async function returnToEventDraft(checkChanges = true) {
  if (!eventDraftReturn.value || busy.value) return false;
  if (checkChanges && formSnapshots.event !== formValue('event')) {
    if (!await dialog.ask({title:'返回原填写？',message:'对原事件的修改尚未保存，可以继续修改或返回原来的填写。',confirmLabel:'返回原填写',cancelLabel:'继续修改',confirmTone:'danger'})) return false;
  }
  const draft = eventDraftReturn.value;
  eventDraftReturn.value = null;
  eventEditingId.value = draft.editingId;
  eventEditingSource.value = draft.source;
  Object.assign(eventForm, draft.values);
  eventFormOpen.value = true;
  eventConflictExpanded.value = false;
  error.value = '';
  await nextTick();
  formSnapshots.event = draft.snapshot;
  return true;
}
async function saveEvent() {
  if (busy.value) return;
  const savedAccount = account.value?.id, savedSignature = identitySignature.value;
  const identityResult = await checkEventIdentity();
  if (!eventFormOpen.value || savedAccount !== account.value?.id || savedSignature !== identitySignature.value || (identityResult && !identityResult.current)) return;
  if (duplicateEvent.value) {
    error.value = '这个事件已经登记，请查看或修改原事件；更改办事日期不会创建新事件。';
    return;
  }
  if (
    !eventForm.host_contact_id ||
    !eventForm.kinship_type_id ||
    !eventForm.event_type_id ||
    (!eventForm.event_date && (!eventEditingId.value || isCompleteEventDate(eventEditingSource.value)))
  ) {
    error.value = "请完整填写事主、亲属称谓、事件类型和办事日期";
    return;
  }
  if (eventForm.event_date && !canCompleteEventDate(eventEditingSource.value, eventForm.event_date)) {
    error.value = '请填写完整有效的办事日期；补齐旧账日期时需保留已知的年、月。';
    return;
  }
  if (eventEditingId.value && !isCompleteEventDate(eventEditingSource.value) && eventForm.event_date) {
    const plannedDate = eventForm.event_date;
    const approved = await dialog.ask({ title:'补齐原事件日期', message:`原办事日期“${eventDate(eventEditingSource.value)}”将补齐为“${plannedDate}”，该事件已有记账的统计日期会一并更新。`, confirmLabel:'确认补齐并保存', cancelLabel:'继续核对' });
    if (!approved || plannedDate !== eventForm.event_date || savedAccount !== account.value?.id || savedSignature !== identitySignature.value || !eventFormOpen.value) return;
  }
  busy.value = true;
  try {
    const body = {
      host_contact_id: Number(eventForm.host_contact_id),
      kinship_type_id: Number(eventForm.kinship_type_id),
      event_type_id: Number(eventForm.event_type_id),
      ...(eventForm.event_date ? { event_date: eventForm.event_date } : {}),
      remark: eventForm.remark.trim() || null,
    };
    const result = eventEditingId.value
      ? await api(`/events/${eventEditingId.value}`, { method: "PATCH", body })
      : await api("/events", { method: "POST", body });
    if (savedAccount !== account.value?.id || savedSignature !== identitySignature.value || !eventFormOpen.value) return;
    const reopenDetail = Boolean(eventEditingId.value);
    const returnToDraft = Boolean(eventDraftReturn.value);
    if (!returnToDraft) eventFormOpen.value = false;
    selectedEvent.value = null;
    await loadEvents();
    if (returnToDraft) {
      busy.value = false;
      await returnToEventDraft(false);
    } else if (reopenDetail && page.value === 'events') {
      selectedEvent.value = result;
      await nextTick();
      const selector = window.innerWidth <= 700 ? '.event-mobile-card' : '.events-table tbody tr';
      const row = document.querySelector(`${selector}.selected`) || [...document.querySelectorAll(selector)].find(element => element.getClientRects().length);
      await eventCard.place(eventCard.anchorFrom({currentTarget:row}));
    }
    success("事件已保存，可随后到记账页登记记账记录");
  } catch (err) {
    if (savedAccount !== account.value?.id || savedSignature !== identitySignature.value) return;
    if (err.code === 'EVENT_ALREADY_EXISTS' && err.details?.existing_event_id) {
      try {
        const detail = await api('/events/' + err.details.existing_event_id);
        if (savedAccount === account.value?.id && savedSignature === identitySignature.value && eventFormOpen.value) {
          invalidateEventIdentity();
          eventIdentityMatch.value = detail;
        }
      } catch (detailError) { popupError(detailError); }
    }
    popupError(err);
  } finally {
    busy.value = false;
  }
}
async function deleteEvent(row) {
  if (busy.value) return;
  if (!(await dialog.ask({title:"删除事件",message:"确认删除这个事件？已有记账记录的事件不能删除。",confirmLabel:"删除",confirmTone:'danger'}))) return;
  busy.value = true;
  try {
    await api(`/events/${row.event.id}`, { method: "DELETE" });
    selectedEvent.value = null;
    await loadEvents();
    success("事件已删除");
  } catch (err) {
    popupError(err);
  } finally { busy.value = false; }
}
async function addDictionary(category) {
  await dialog.ask({title:'自定义' + dictionaryNames[category],message:'新建后仅在你的账本中使用。',input:true,inputLabel:dictionaryNames[category],confirmLabel:'添加',onConfirm:async(name)=> {
    if(!name) throw new Error('请输入名称');
    const item = await api('/dictionaries/' + category,{method:'POST',body:{name}});
    dictionaries[category].push(item);
    success('自定义选项已添加');
  }});
}

function ledgerParams() {
  return {
    keyword: ledgerSearch.value.trim(),
    relationship_type_ids: ledgerFilters.relations,
    kinship_type_ids: ledgerFilters.kinships,
    event_type_ids: ledgerFilters.types,
    date_from: ledgerFilters.date_from,
    date_to: ledgerFilters.date_to,
    ...ledgerContextParams(ledgerContext.value),
    sort_by: ledgerSort.sort_by,
    sort_order: ledgerSort.sort_order,
    limit: ledgerPageSize.value,
    offset: (ledgerPage.value - 1) * ledgerPageSize.value,
  };
}
async function loadLedger(throwOnError = false) {
  clearTimeout(ledgerSearchTimer);
  const request = ++ledgerRequest;
  if (ledgerFilters.date_from && ledgerFilters.date_to && ledgerFilters.date_from > ledgerFilters.date_to) { ledgerLoading.value = false; popupError(new Error('开始日期不能晚于结束日期')); return; }
  ledgerLoading.value = true; ledgerQueryError.value='';
  try {
    const result = await api('/ledger/query/gifts' + queryString(ledgerParams()));
    if (request === ledgerRequest) {
      invalidateDetailAvailability();
      ledger.value = result;
      const maxPage=Math.max(1,Math.ceil((result.page.total||0)/ledgerPageSize.value));
      if(ledgerPage.value>maxPage){ledgerPage.value=maxPage;return await loadLedger(throwOnError);}
    }
  } catch(err) { if(request === ledgerRequest) { ledgerQueryError.value=err.message || '记账加载失败，请重试';if(throwOnError)throw err;popupError(err); } }
  finally { if(request === ledgerRequest) ledgerLoading.value = false; }
}
async function searchLedger() {
  ledgerPage.value = 1;
  await loadLedger();
}
async function openLedgerForStatistics(context) {
  const params=JSON.parse(JSON.stringify(context.params));const from=params.date_from||'',to=params.date_to||'',keyword=params.keyword||'';
  delete params.date_from;delete params.date_to;delete params.keyword;
  if (params.event_type_ids?.length) { params.statistics_event_type_ids=params.event_type_ids;delete params.event_type_ids; }
  await pageNavigation.associate('ledger',{title:'来自统计页的记账明细'},()=>{
    ledgerSearch.value=keyword;Object.assign(ledgerFilters,{relations:[],kinships:[],types:[],date_from:from,date_to:to});
    ledgerContext.value={kind:'statistics',params,label:context.label,eventLabel:(context.label||'').replace(/\s*·\s*(\d{4}-\d{2}-\d{2} 至 \d{4}-\d{2}-\d{2}|全部历史).*$/,'')};ledgerPage.value=1;
  });
}
/** 从事件卡跳转：按事件 ID 精确筛选记账记录。 */
async function openLedgerForEvent(eventId,label) {
  if(!eventId || !await allowDetailLink('eventGifts', eventId))return;
  await pageNavigation.associate('ledger',{title:'仅看事件：'+label},()=>{
    ledgerSearch.value='';Object.assign(ledgerFilters,{relations:[],kinships:[],types:[],date_from:'',date_to:''});
    ledgerContext.value={kind:'event',id:eventId,label};ledgerPage.value=1;
  });
}
/** 从联系人卡跳转：按联系人 ID 精确筛选记账记录。 */
async function openLedgerForContact(contact) {
  if(!contact?.id || !await allowDetailLink('gifts', contact.id))return;
  const label=queryContactLabel(contact);
  await pageNavigation.associate('ledger',{title:'仅看与 '+label+' 相关的记账'},()=>{
    ledgerSearch.value='';Object.assign(ledgerFilters,{relations:[],kinships:[],types:[],date_from:'',date_to:''});
    ledgerContext.value={kind:'contact',id:contact.id,label};ledgerPage.value=1;
  });
}
async function toggleLedgerGift(gift, event) {
  if (ledgerGift.value?.id === gift.id) {
    ledgerGift.value = null;
    return;
  }
  ledgerRowMenuId.value = null;
  ledgerGift.value = gift;
  giftCard.position.left = 12;
  giftCard.position.top = 12;
  await giftCard.place(giftCard.anchorFrom(event));
}
function toggleLedgerRowMenu(giftId) {
  ledgerRowMenuId.value = ledgerRowMenuId.value === giftId ? null : giftId;
}
/** 礼物列太窄，用固定定位的浮层展示全文：鼠标悬停、键盘聚焦、手机点按都可看。 */
function showGiftTip(text, event) {
  if (!text) return;
  const rect = event.currentTarget.getBoundingClientRect();
  giftTip.value = {
    text,
    left: Math.max(8, Math.min(rect.left, window.innerWidth - 288)),
    top: rect.bottom + 6,
  };
}
function hideGiftTip() {
  if (giftTip.value.text) giftTip.value = { text: "", left: 0, top: 0 };
}
/** 从事件卡跳转到“记一笔”并为该事件预选。 */
async function addGiftToEvent(eventId) {
  if (!eventId) return;
  if (await navigate("ledger")) openEntry(eventId);
}
async function openEntry(eventId = null) {
  error.value = "";
  closeFloatingCards();
  entryLoading.value = true;
  entryOpen.value = true;
  entryEvent.value = null;
  entryCards.value = [blankCard()];
  entryEventSearch.value = "";
  entryConflictExpanded.value = false;
  entryConflictFromServer.value = null;
  try {
    entryEventOptions.value = await allPages("/events");
    entryEventOptions.value.sort(
      (a, b) =>
        (b.event.created_at || "").localeCompare(a.event.created_at || "") ||
        b.event.id - a.event.id,
    );
    if (!entryOpen.value) return;
    if (eventId) await chooseEntryEvent(eventId);
    if (eventId) formSnapshots.entry = formValue('entry');
  } catch (err) {
    popupError(err);
  } finally { entryLoading.value = false; }
}
async function chooseEntryEvent(id) {
  entryConflictFromServer.value = null;
  if (entrySelecting.value || entryEvent.value?.event.id === id) return;
  if (entryEvent.value && entryCards.value.some(card => card.amount !== "" || card.gift_details || card.remark || (entryIsSelf.value && card.giver_contact_id))) {
    const discard = await dialog.ask({title:"切换事件？",message:"当前随礼人填写内容尚未保存，切换会清空这些内容。",confirmLabel:"放弃并切换",cancelLabel:"继续填写",confirmTone:'danger'});
    if(!discard) return;
  }
  entrySelecting.value = true;
  try {
    entryEvent.value = await api(`/events/${id}`);
    entryCompleteDate.value = '';
    entryCards.value = [blankCard()];
    if (entryEvent.value.event.host.id !== selfContact.value?.id)
      entryCards.value[0].giver_contact_id = selfContact.value?.id || null;
  } catch (err) {
    popupError(err);
  } finally { entrySelecting.value = false; }
}
function addGiverCard() {
  if (entryIsSelf.value && entryCards.value.length < 5)
    entryCards.value.push(blankCard());
}
function giverExcludes(index) {
  return entryCards.value
    .filter((_, i) => i !== index)
    .map((x) => x.giver_contact_id)
    .filter(Boolean);
}
async function saveEntry() {
  const savedAccount = account.value?.id, savedEventId = entryEvent.value?.event.id;
  if (busy.value || entryLoading.value || entrySelecting.value) return;
  if (!entryEvent.value) {
    error.value = "请先选择已有事件";
    return;
  }
  if (!isCompleteEventDate(entryEvent.value)) {
    error.value = '这个旧账事件的办事日期不完整，请先补齐完整日期后再新增记账。';
    return;
  }
  if (entryDuplicateGift.value) {
    error.value = "这个事件已登记本人记账记录，请修改原记账记录";
    return;
  }
  if (
    entryCards.value.some(
      (card) =>
        !card.giver_contact_id ||
        card.amount === "" ||
        !Number.isFinite(Number(card.amount)) ||
        Number(card.amount) < 0 ||
        (Number(card.amount) === 0 && !card.gift_details.trim()),
    )
  ) {
    error.value = "请按要求填完所有随礼人卡片，或关闭不用的卡片";
    return;
  }
  if (
    new Set(entryCards.value.map((x) => x.giver_contact_id)).size !==
    entryCards.value.length
  ) {
    error.value = "同一事件中的随礼人不能重复";
    return;
  }
  busy.value = true;
  try {
    const gifts = entryCards.value.map((x) => ({
      giver_contact_id: x.giver_contact_id,
      amount: Number(x.amount),
      gift_details: x.gift_details.trim() || null,
      remark: x.remark.trim() || null,
    }));
    await api(`/events/${entryEvent.value.event.id}/gifts`, {
      method: "POST",
      body: { gifts },
    });
    if (savedAccount !== account.value?.id || savedEventId !== entryEvent.value?.event.id || !entryOpen.value) return;
    entryOpen.value = false;
    await loadLedger();
    success(`已保存 ${gifts.length} 笔记账记录`);
  } catch (err) {
    if (savedAccount !== account.value?.id || savedEventId !== entryEvent.value?.event.id || !entryOpen.value) return;
    if (err.code === 'GIFT_ALREADY_EXISTS' && err.details?.existing_gift_id) {
      try {
        const detail = await api('/gifts/' + err.details.existing_gift_id);
        if (savedAccount === account.value?.id && savedEventId === entryEvent.value?.event.id && entryOpen.value) entryConflictFromServer.value = detail.gift;
      } catch (detailError) { popupError(detailError); }
    }
    popupError(err);
  } finally {
    busy.value = false;
  }
}
async function completeEntryDate() {
  const detail = entryEvent.value;
  if (busy.value || !detail) return;
  if (!canCompleteEventDate(detail, entryCompleteDate.value)) {
    error.value = '请填写完整有效日期，并保留这个旧账事件已知的年份、月份。';
    return;
  }
  const completeDate = entryCompleteDate.value;
  const savedAccount = account.value?.id;
  if (!await dialog.ask({ title:'补齐原事件日期', message:`原办事日期为“${eventDate(detail)}”，将补齐为“${completeDate}”。该事件下 ${detail.gifts?.length || 0} 笔已有记账会使用补齐后的日期进行统计。`, confirmLabel:'确认补齐', cancelLabel:'继续核对' })) return;
  busy.value = true;
  try {
    const updated = await api(`/events/${detail.event.id}`, { method:'PATCH', body:{ event_date:completeDate } });
    if (savedAccount !== account.value?.id || entryEvent.value?.event.id !== detail.event.id) return;
    entryEvent.value = updated;
    entryCompleteDate.value = '';
    entryEventOptions.value = entryEventOptions.value.map(row => row.event.id === detail.event.id ? updated : row);
    await loadEvents();
    success('原事件日期已补齐，现在可以新增记账。');
  } catch (err) { popupError(err); }
  finally { busy.value = false; }
}
function requestReauth() {
  reauthPassword.value = "";
  reauthError.value = "";
  reauthBusy.value = false;
  reauthOpen.value = true;
  return new Promise((resolve) => {
    reauthResolve = resolve;
  });
}
function closeReauth(confirmed) {
  reauthOpen.value = false;
  reauthPassword.value = "";
  reauthError.value = "";
  const resolve = reauthResolve;
  reauthResolve = null;
  if (resolve) resolve(confirmed);
}
async function submitReauth() {
  if (reauthBusy.value) return;
  if (!reauthPassword.value) {
    reauthError.value = "请输入当前账号密码";
    return;
  }
  reauthBusy.value = true;
  reauthError.value = "";
  try {
    // 密码只发给服务端做一次再验证，不写入任何本地存储。
    await api("/auth/gift-changes/verify", {
      method: "POST",
      body: { password: reauthPassword.value },
    });
    closeReauth(true);
  } catch (err) {
    reauthError.value = err.message || "密码验证失败，请重试";
  } finally {
    reauthBusy.value = false;
  }
}
/**
 * 修改／删除记账记录的所有入口共用：先按原样请求一次，
 * 只有服务端明确要求密码再验证时才弹框，验证成功后再重试。
 */
async function withReauth(action) {
  try {
    return { ok: true, value: await action() };
  } catch (err) {
    if (!isReauthError(err)) throw err;
  }
  const confirmed = await requestReauth();
  if (!confirmed) return { ok: false, cancelled: true };
  return { ok: true, value: await action() };
}
async function openGiftEditor(gift, eventId = null) {
  const editingAccount = account.value?.id;
  error.value = "";
  closeFloatingCards();
  ledgerRowMenuId.value = null;
  try {
    const detail = await api(`/gifts/${gift.id}`);
    if (editingAccount !== account.value?.id) return;
    const event = await api(`/events/${eventId || detail.event.id}`);
    if (editingAccount !== account.value?.id) return;
    giftEditDetail.value = detail;
    giftEditEvent.value = event;
    Object.assign(giftEditForm, {
      giver_contact_id: detail.gift.giver.id,
      amount: String(detail.gift.amount),
      gift_details: detail.gift.gift_details || "",
      remark: detail.gift.remark || "",
    });
    giftEditOpen.value = true;
  } catch (err) {
    if (editingAccount !== account.value?.id) return;
    popupError(err);
  }
}
async function editDuplicateGift() {
  const gift = entryDuplicateGift.value;
  if (!gift || busy.value) return;
  const savedAccount = account.value?.id;
  entryDraftReturn.value = {
    eventId: entryEvent.value.event.id,
    cards: entryCards.value.map(card => ({ ...card })),
    search: entryEventSearch.value,
    snapshot: formSnapshots.entry,
  };
  const draft = entryDraftReturn.value;
  entryOpen.value = false;
  await nextTick();
  if (savedAccount !== account.value?.id || entryDraftReturn.value !== draft) return;
  await openGiftEditor(gift, draft.eventId);
  if (savedAccount !== account.value?.id) return;
  if (!giftEditOpen.value) await restoreEntryDraft();
}
async function restoreEntryDraft() {
  const draft = entryDraftReturn.value;
  if (!draft) return;
  const savedAccount = account.value?.id;
  let detail = entryEvent.value;
  let refreshError = null;
  try { detail = await api(`/events/${draft.eventId}`); }
  catch (err) { refreshError = err; }
  if (savedAccount !== account.value?.id || draft !== entryDraftReturn.value) return;
  entryDraftReturn.value = null;
  entryConflictFromServer.value = null;
  entryEvent.value = detail;
  entryCards.value = draft.cards.map(card => ({ ...card }));
  entryEventSearch.value = draft.search;
  entryOpen.value = true;
  await nextTick();
  formSnapshots.entry = draft.snapshot;
  if (refreshError) popupError(refreshError);
}
async function saveGiftEdit() {
  if (busy.value) return;
  const amount = Number(giftEditForm.amount);
  if (
    !Number.isFinite(amount) ||
    giftEditForm.amount === "" ||
    amount < 0 ||
    (amount === 0 && !giftEditForm.gift_details.trim()) ||
    !giftEditForm.giver_contact_id
  ) {
    error.value = "请填写随礼人和礼金；纯实物时须填写实物明细";
    return;
  }
  busy.value = true;
  try {
    const body = {
      amount,
      gift_details: giftEditForm.gift_details.trim() || null,
      remark: giftEditForm.remark.trim() || null,
    };
    if (giftEditIsSelf.value)
      body.giver_contact_id = giftEditForm.giver_contact_id;
    const result = await withReauth(() =>
      api(`/gifts/${giftEditDetail.value.gift.id}`, {
        method: "PATCH",
        body,
      }),
    );
    if (result.cancelled) return;
    giftEditOpen.value = false;
    ledgerGift.value = null;
    await loadLedger();
    await restoreEntryDraft();
    success("记账记录已更正");
  } catch (err) {
    popupError(err);
  } finally {
    busy.value = false;
  }
}
async function deleteGift(gift) {
  if (busy.value) return;
  if (!(await dialog.ask({title:"删除记账记录",message:`确认删除 ${gift.giver?.name || "这位随礼人"} 的这笔记账记录？`,confirmLabel:"删除",confirmTone:'danger'}))) return;
  busy.value = true;
  try {
    const result = await withReauth(() =>
      api(`/gifts/${gift.id}`, { method: "DELETE" }),
    );
    if (result.cancelled) return;
    giftEditOpen.value = false;
    ledgerGift.value = null;
    ledgerRowMenuId.value = null;
    await loadLedger();
    success("记账记录已删除");
  } catch (err) {
    popupError(err);
  } finally { busy.value = false; }
}
const dictionaryNames = {
  relationship: "关系类型",
  kinship: "亲属称谓",
  event: "事件类型",
};
async function reloadDictionary(category) {
  const result = await api(`/dictionaries/${category}?include_inactive=true`);
  dictionaries[category] = result.items;
}
async function createDictionaryItem() {
  if (busy.value) return;
  const name = dictionaryDraft.value.trim();
  if (!name) return;
  busy.value = true;
  try {
    await api(`/dictionaries/${dictionaryCategory.value}`, {
      method: "POST",
      body: { name },
    });
    dictionaryDraft.value = "";
    await reloadDictionary(dictionaryCategory.value);
    success("自定义字典项已添加");
  } catch (err) {
    popupError(err);
  } finally { busy.value = false; }
}
async function saveDictionaryRename(item) {
  if (busy.value) return;
  const name = dictionaryRename.name.trim();
  if (!name) { popupError(new Error('请输入自定义选项名称')); return; }
  busy.value = true;
  try {
    await api(`/dictionaries/${dictionaryCategory.value}/${item.id}`, {
      method: "PATCH",
      body: { name },
    });
    dictionaryRename.id = null;
    await reloadDictionary(dictionaryCategory.value);
    success("名称已更正");
  } catch (err) {
    popupError(err);
  } finally { busy.value = false; }
}
async function cancelDictionaryRename(item) {
  if (busy.value) return false;
  if (item && dictionaryRename.name !== item.name && !(await dialog.ask({title:'放弃更改？',message:'自定义选项名称还没有保存。',confirmLabel:'放弃更改',cancelLabel:'继续填写',confirmTone:'danger'}))) return false;
  dictionaryRename.id = null;
  return true;
}
async function setDictionaryActive(item) {
  if (busy.value) return;
  busy.value = true;
  try {
    await api(
      `/dictionaries/${dictionaryCategory.value}/${item.id}/${item.is_active ? "deactivate" : "activate"}`,
      { method: "POST" },
    );
    await reloadDictionary(dictionaryCategory.value);
    success(item.is_active ? "字典项已停用" : "字典项已启用");
  } catch (err) {
    popupError(err);
  } finally { busy.value = false; }
}
async function deleteDictionary(item) {
  if (busy.value) return;
  if (!(await dialog.ask({title:"删除自定义选项",message:`确认删除“${item.name}”？被使用的选项不能删除。`,confirmLabel:"删除",confirmTone:'danger'}))) return;
  busy.value = true;
  try {
    await api(`/dictionaries/${dictionaryCategory.value}/${item.id}`, {
      method: "DELETE",
    });
    await reloadDictionary(dictionaryCategory.value);
    success("字典项已删除");
  } catch (err) {
    popupError(err);
  } finally { busy.value = false; }
}
async function previewOldLedger() {
  if (busy.value || importResultUncertain.value) return;
  if (!importFile.value) {
    error.value = "请先选择 .xlsx 旧账文件";
    return;
  }
  busy.value = true;
  importAcknowledged.value = false;
  const savedFile = importFile.value, savedAccount = account.value?.id;
  try {
    const preview = await upload("/imports/preview", savedFile, { decisions: importDecisions.value });
    if (savedAccount !== account.value?.id || savedFile !== importFile.value) return;
    importPreview.value = preview;
    importDecisionsDirty.value = false;
    importResult.value = null;
    success(
      preview.error_rows
        ? "预检发现问题，请按下面提示修改文件后重新上传"
        : canImport(preview) ? "预检通过，请核对逐行计划和汇总后确认整批导入" : "请完成待确认项，再按选择重新预检",
    );
  } catch (err) {
    popupError(err);
  } finally {
    busy.value = false;
  }
}
async function confirmOldLedger() {
  if (busy.value) return;
  if (!canImport(importPreview.value) || importDecisionsDirty.value || (importPreview.value?.issues && !importAcknowledged.value)) return;
  const token = importPreview.value.preview_token;
  const authorization = await requestSensitiveAuthorization('legacy_import', { preview_token: token }, '旧账导入');
  if (!authorization || authorization.marker !== loginMarker() || importPreview.value?.preview_token !== token || importDecisionsDirty.value || !canImport(importPreview.value)) return;
  busy.value = true;
  rememberImportRecovery(token);
  try {
    const result = await api(`/imports/${encodeURIComponent(token)}/confirm`, {
      method: "POST",
      headers: { 'X-Operation-Grant': authorization.grant },
    });
    if (authorization.marker !== loginMarker()) return;
    await finishOldLedgerImport(result);
  } catch (err) {
    if (authorization.marker !== loginMarker()) return;
    if ([401,403,429].includes(err.status)) { clearImportRecovery();popupError(err); }
    else if (err.code === 'IMPORT_STATE_CHANGED' || err.code === 'IMPORT_VALIDATION_FAILED' || err.status === 422) {
      importAcknowledged.value = false;
      importDecisionsDirty.value = true;
      clearImportRecovery();
      popupError(err);
    } else {
      importResultUncertain.value = true;
      error.value = '本次确认结果尚未查明，请点击“查询本批结果”。请先核实这批记录，不要重新上传导入。';
    }
  } finally {
    busy.value = false;
  }
}
function rememberImportRecovery(token) {
  importRecoveryToken.value = token;
  importRecoveryUnavailable.value = false;
  sessionStorage.setItem('gift_ledger_import_result_token', token);
  sessionStorage.setItem('gift_ledger_import_result_user', String(account.value?.id));
}
function clearImportRecovery() {
  importRecoveryToken.value = '';
  importResultUncertain.value = false;
  importRecoveryUnavailable.value = false;
  sessionStorage.removeItem('gift_ledger_import_result_token');
  sessionStorage.removeItem('gift_ledger_import_result_user');
}
async function finishOldLedgerImport(result) {
  importResult.value = result;
  importPreview.value = null;
  importFile.value = null;
  importDecisions.value = {};
  importAcknowledged.value = false;
  importDecisionsDirty.value = false;
  clearImportRecovery();
  let refreshFailed = false;
  try { await loadCore(); }
  catch { refreshFailed = true; }
  await refreshImportBatches();
  success(`旧账已导入：${result.imported_contacts} 位联系人、${result.imported_events} 个事件、${result.imported_gifts} 笔记账记录`);
  if (refreshFailed) error.value = '旧账已成功导入，但页面资料刷新失败，请刷新页面查看；无需再次导入。';
}
async function queryImportResult() {
  if (busy.value || !importRecoveryToken.value) return;
  busy.value = true;
  const token = importRecoveryToken.value, savedAccount = account.value?.id;
  try {
    const status = await api(`/imports/${encodeURIComponent(token)}/result`);
    if (savedAccount !== account.value?.id) return;
    importRecoveryUnavailable.value = false;
    if (status.status === 'completed') await finishOldLedgerImport(status.result);
    else if (status.status === 'pending') {
      error.value = '';
      notice.value = '服务器确认这批尚未导入，可点击“继续确认本批”重试同一批次。';
      importResult.value = { status:'pending' };
    } else {
      notice.value = '服务器仍在处理这批记录，请稍后再次查询。';
      importResult.value = { status:'processing' };
    }
  } catch (err) {
    await refreshImportBatches();
    importRecoveryUnavailable.value = [404,410].includes(err.status) && !importBatchError.value;
    error.value = `${err.message}。请先核对下面的最近导入记录；未核实前不要重新上传。`;
  } finally { busy.value = false; }
}
async function returnImportToPreview() {
  if (busy.value || !importRecoveryUnavailable.value) return;
  const approved = await dialog.ask({title:'返回旧账预检',message:'请先核对最近导入记录和账本。本批凭证已不能查询，是否已完成仍不确定；返回后必须重新预检，重复记账会被拦截，原记录不会被覆盖。',confirmLabel:'已核对，返回预检',cancelLabel:'继续核对'});
  if (!approved) return;
  clearImportRecovery();
  importResult.value = null;
  importAcknowledged.value = false;
  importDecisionsDirty.value = true;
  notice.value = '请重新预检。若提示已有同一事件、同一随礼人的记账，请查看原记录核对。';
  error.value = '';
}
async function retryImportConfirmation() {
  if (busy.value || !importRecoveryToken.value || importResult.value?.status !== 'pending') return;
  const token=importRecoveryToken.value;
  const authorization = await requestSensitiveAuthorization('legacy_import', { preview_token: token }, '继续确认本批旧账导入');
  if (!authorization || authorization.marker !== loginMarker() || importRecoveryToken.value !== token || importResult.value?.status !== 'pending') return;
  busy.value = true;
  try {
    const result=await api(`/imports/${encodeURIComponent(token)}/confirm`, { method:'POST', headers: { 'X-Operation-Grant': authorization.grant } });
    if (authorization.marker === loginMarker()) await finishOldLedgerImport(result);
  }
  catch (err) {
    if (authorization.marker !== loginMarker()) return;
    if ([401,403,429].includes(err.status)) popupError(err);
    else if (['IMPORT_STATE_CHANGED','IMPORT_VALIDATION_FAILED'].includes(err.code)) {
      importDecisionsDirty.value = true; importAcknowledged.value = false; clearImportRecovery(); popupError(err);
    } else { importResultUncertain.value = true; importResult.value = null; error.value = '本批结果尚未查明，请再次查询本批结果。'; }
  } finally { busy.value = false; }
}
async function refreshImportBatches() {
  if (importBatchLoading.value || !account.value?.id) return;
  const savedAccount = account.value.id;
  importBatchLoading.value = true;
  try {
    const response = await api('/imports/batches');
    if (savedAccount === account.value?.id) { importBatches.value = response.batches || []; importBatchError.value = ''; }
  } catch (err) { if (savedAccount === account.value?.id) importBatchError.value = err.message; }
  finally { if (savedAccount === account.value?.id) importBatchLoading.value = false; }
}
function changeImportDecision(key, value) { setImportDecision(meState, key, value); }
async function openImportIssue(issue, edit = false) {
  const savedAccount = account.value?.id;
  try {
    if (issue.existing_gift_id) {
      const detail = await api(`/gifts/${issue.existing_gift_id}`);
      if (savedAccount !== account.value?.id || !await navigate('ledger')) return;
      if (edit) await openGiftEditor(detail.gift, detail.event.id);
      else { ledgerGift.value = { ...detail.gift, event:detail.event }; await giftCard.place(null); }
    } else if (issue.existing_event_id) {
      const detail = await api(`/events/${issue.existing_event_id}`);
      if (savedAccount !== account.value?.id || !await navigate('events')) return;
      if (edit) openEventForm(detail);
      else { selectedEvent.value = detail; await eventCard.place(null); }
    }
    notice.value = '旧账文件和预检已保留。核对或修改原资料后，请回“我的 → 旧账导入”重新预检。';
    importDecisionsDirty.value = true;
    importAcknowledged.value = false;
  } catch (err) { popupError(err); }
}
async function previewRestore() {
  if (busy.value) return;
  if (!restoreFile.value) {
    error.value = "请先选择 .giftledger 备份文件";
    return;
  }
  busy.value = true;
  restorePreview.value = null;
  try {
    restorePreview.value = await upload(
      "/restores/personal/preview",
      restoreFile.value,
    );
    restoreRenames.value = restorePreview.value.dictionary_name_conflicts.map(
      (item) => ({
        category: item.category,
        backup_id: item.backup_id,
        original_name: item.original_name,
        new_name: item.suggested_name,
      }),
    );
    restoreAcknowledged.value = false;
    restorePassword.value = "";
    success("备份预检通过，请核对替换范围");
  } catch (err) {
    popupError(err);
  } finally {
    busy.value = false;
  }
}
async function confirmRestore() {
  if (busy.value) return;
  if (
    !restorePreview.value ||
    !restoreAcknowledged.value ||
    !restorePassword.value
  )
    return;
  if (restoreRenames.value.some((item) => !item.new_name.trim())) {
    error.value = "请为冲突的自定义字典填写新名称";
    return;
  }
  busy.value = true;
  const token = restorePreview.value.preview_token;
  try {
    const result = await api(
      `/restores/personal/${encodeURIComponent(token)}/confirm`,
      {
        method: "POST",
        body: {
          password: restorePassword.value,
          dictionary_renames: restoreRenames.value.map((item) => ({
            ...item,
            new_name: item.new_name.trim(),
          })),
        },
      },
    );
    safetyBackupToken.value = result.safety_backup_token;
    sessionStorage.setItem(
      "gift_ledger_safety_token",
      result.safety_backup_token,
    );
    sessionStorage.setItem(
      "gift_ledger_safety_user_id",
      String(account.value.id),
    );
    restorePreview.value = null;
    restoreFile.value = null;
    restorePassword.value = "";
    account.value = null;
    profile.value = null;
    authMode.value = "login";
    success("恢复成功。请重新登录，之后可在“我的”下载恢复前安全备份。");
  } catch (err) {
    restorePreview.value = null;
    restorePassword.value = "";
    popupError(err);
  } finally {
    busy.value = false;
  }
}
async function saveMyProfile() {
  if (busy.value) return;
  busy.value = true;
  try {
    profile.value = await api("/me/profile", {
      method: "PATCH",
      body: { name: profileForm.name.trim() },
    });
    account.value = await api("/auth/me");
    await loadPeople();
    success("本人信息已更新");
  } catch (err) {
    popupError(err);
  } finally { busy.value = false; }
}
watch(profile, (x) => {
  if (x) profileForm.name = x.name;
});
const authState = reactive({account,authMode,credentials,profileForm,busy,error,notice});
const authActions = {authSubmit,saveProfile};
const meState = reactive({profileForm,meSection,busy,dictionaries,dictionaryCategory,dictionaryDraft,dictionaryRename,importFile,importPreview,importDecisions,importDecisionsDirty,importAcknowledged,importResult,importResultUncertain,importRecoveryUnavailable,importBatches,importBatchLoading,importBatchError,restoreFile,restorePreview,restorePassword,restoreAcknowledged,restoreRenames,safetyBackupToken,error,notice});
const meActions = Object.fromEntries(Object.entries({saveMyProfile,exportAllData,downloadPersonalBackup,downloadSafetyBackup,previewOldLedger,confirmOldLedger,changeImportDecision,openImportIssue,queryImportResult,retryImportConfirmation,returnImportToPreview,refreshImportBatches,previewRestore,confirmRestore,createDictionaryItem,saveDictionaryRename,cancelDictionaryRename,setDictionaryActive,deleteDictionary,logout}).map(([name, action]) => [name, (...args) => {
  if (busy.value) return;
  error.value = '';
  notice.value = '';
  return action(...args);
}]));
function formValue(kind) {
  if(kind === 'contact') return JSON.stringify(contactForm);
  if(kind === 'event') return JSON.stringify(eventForm);
  if(kind === 'gift') return JSON.stringify(giftEditForm);
  return JSON.stringify({event:entryEvent.value?.event.id || null,cards:entryCards.value});
}
const formOpenFlags = {contact:contactFormOpen,event:eventFormOpen,gift:giftEditOpen,entry:entryOpen};
for(const [kind,flag] of Object.entries(formOpenFlags)) watch(flag,(open)=> {
  if(open) { formSnapshots[kind] = formValue(kind); document.dispatchEvent(new Event('ledger-close-menus')); }
},{flush:'post'});
async function closeForm(kind) {
  if(busy.value || entrySelecting.value) return false;
  if (kind === 'event' && eventDraftReturn.value) { await returnToEventDraft(); return false; }
  if(formSnapshots[kind] !== formValue(kind)) {
    const discard = await dialog.ask({title:'放弃未保存的更改？',message:'填写内容尚未保存，你可以继续填写或放弃更改。',confirmLabel:'放弃更改',cancelLabel:'继续填写',confirmTone:'danger'});
    if(!discard) return false;
  }
  formOpenFlags[kind].value = false;
  error.value = '';
  if (kind === 'gift' && entryDraftReturn.value) { await restoreEntryDraft(); return false; }
  return true;
}
async function viewSameName(match,event) { if(await closeForm('contact')) await togglePerson(match,event); }

function onGlobalPointerDown(event) {
  if (event.target.classList?.contains('drawer-backdrop')) { dismissFloatingCards(); return; }
  if (!event.target.closest?.('.float-card,.data-table tr,.mobile-card,.recent-card')) closeFloatingCards();
  if (!event.target.closest?.('.row-menu')) ledgerRowMenuId.value = null;
}
function onKeydown(event) {
  if (event.key !== "Escape") return;
  if(event.target.closest?.('.option-menu,.picker-menu') || event.target.getAttribute?.('aria-expanded') === 'true') return;
  if(dialog.state.open) { dialog.answer(false); return; }
  if(guideOpen.value) { guideOpen.value=false; return; }
  if(sensitiveOpen.value) { if (!sensitiveBusy.value) closeSensitiveAuthorization(); return; }
  if(reauthOpen.value) { if (!reauthBusy.value) closeReauth(false); return; }
  for(const [kind,flag] of Object.entries(formOpenFlags)) { if(flag.value) { closeForm(kind); return; } }
  if (ledgerRowMenuId.value) {
    ledgerRowMenuId.value = null;
    return;
  }
  if (giftTip.value.text) {
    hideGiftTip();
    return;
  }
  if (selectedPerson.value || selectedEvent.value || ledgerGift.value)
    dismissFloatingCards();
}
const listOverlayOpen = computed(() => floatingCardOpen.value || guideOpen.value || peopleFilterOpen.value || eventFilterOpen.value || ledgerFilterOpen.value || Object.values(formOpenFlags).some(flag => flag.value) || reauthOpen.value || sensitiveOpen.value || dialog.state.open);
function dateFilterError(filters){return filters.date_from && filters.date_to && filters.date_from > filters.date_to ? '开始日期不能晚于结束日期' : ''; }
function clearFilters(kind){const filters=kind==='people'?peopleFilters:kind==='events'?eventFilters:ledgerFilters;for(const key of Object.keys(filters))filters[key]=Array.isArray(filters[key])?[]:'';}
watch(page,()=>{guideOpen.value=false;peopleFilterOpen.value=eventFilterOpen.value=ledgerFilterOpen.value=false;});
const peopleSortOptions = [ ['created_at','添加日期'],['name','姓名'],['relationship','关系'],['remark','联系人备注'],['flow','来往状态'],['expense','我随出'],['income','我收到'],['balance','差额'] ].map(([value,label])=>({value,label}));
const eventSortOptions = [ ['registered_at','登记日期'],['event_date','办事日期'],['host_name','事主姓名'],['gift_count','记账记录笔数'],['cash_amount','礼金合计'] ].map(([value,label])=>({value,label}));
const ledgerSortOptions = [ ['gift_created_at','登记日期'],['event_date','办事日期'],['host_name','事主姓名'],['giver_name','随礼人姓名'],['amount','礼金金额'] ].map(([value,label])=>({value,label}));
function clearSearch(kind) {
  if(kind === 'people') { peopleContext.value=null;peopleSearch.value = ''; appliedPeopleSearch.value = ''; Object.assign(peopleFilters,{relations:[],statuses:[],flows:[],date_from:'',date_to:''}); peoplePage.value = 1; }
  if(kind === 'events') { eventContext.value=null;eventSearch.value = ''; Object.assign(eventFilters,{relations:[],kinships:[],types:[],date_from:'',date_to:''}); searchEvents(); }
  if(kind === 'ledger') { ledgerContext.value=null;ledgerSearch.value = ''; Object.assign(ledgerFilters,{relations:[],kinships:[],types:[],date_from:'',date_to:''}); searchLedger(); }
  pageNavigation.clear();
}
function searchPeople() { clearTimeout(peopleSearchTimer); appliedPeopleSearch.value = peopleSearch.value; peoplePage.value = 1; }
watch(peopleSearch,()=> { clearTimeout(peopleSearchTimer);if(restoringPage.value)return;peopleSearchTimer = setTimeout(searchPeople,250); });
watch(eventSearch,()=> { clearTimeout(eventSearchTimer); if(!restoringPage.value && page.value === 'events' && account.value?.profile_completed) eventSearchTimer = setTimeout(searchEvents,250); });
watch(ledgerSearch,()=> { clearTimeout(ledgerSearchTimer); if(!restoringPage.value && page.value === 'ledger' && account.value?.profile_completed) ledgerSearchTimer = setTimeout(searchLedger,250); });
watch(eventFilters,()=> { if(!restoringPage.value && page.value === 'events' && account.value?.profile_completed) searchEvents(); },{deep:true});
watch(ledgerFilters,()=> { if(!restoringPage.value && page.value === 'ledger' && account.value?.profile_completed) searchLedger(); },{deep:true});
function sortArrow(sort,field) { return sort.sort_by === field ? (sort.sort_order === 'asc' ? '▲' : '▼') : '↕'; }
function sortRemote(kind,field) {
  const sort = kind === 'events' ? eventSort : ledgerSort;
  if(sort.sort_by === field) sort.sort_order = sort.sort_order === 'asc' ? 'desc' : 'asc';
  else { sort.sort_by = field; sort.sort_order = /date|registered|created/.test(field) ? 'desc' : 'asc'; }
  kind === 'events' ? searchEvents() : searchLedger();
}
function personActions(person) { return [{id:'edit',label:person.is_self?'修改本人信息':'修改联系人'},{id:'active',label:person.is_active?'停用联系人':'启用联系人',disabled:person.is_self},{id:'delete',label:'删除联系人',danger:true,disabled:person.is_self || person.has_references}]; }
function personAction(action,person) { if(action === 'edit') openContact(person); if(action === 'active') setContactActive(person); if(action === 'delete') deleteContact(person); }
function eventAction(action,row) { if(action === 'edit') openEventForm(row); if(action === 'delete') deleteEvent(row); }
function giftAction(action,gift) { if(action === 'edit') openGiftEditor(gift); if(action === 'delete') deleteGift(gift); }
onMounted(() => {
  readMaintenanceAccess().then(available => { maintenanceAvailable.value = available; });
  bootstrap();
  window.addEventListener("resize", keepPersonCardInView);
  document.addEventListener("keydown", onKeydown);
  document.addEventListener("pointerdown", onGlobalPointerDown);
  document.addEventListener('ledger-close-details', closeFloatingCards);
});
onUnmounted(() => {
  closeSensitiveAuthorization();
  invalidateEventIdentity();
  invalidateDetailAvailability();
  pageNavigation.destroy();
  clearTimeout(peopleSearchTimer); clearTimeout(eventSearchTimer); clearTimeout(ledgerSearchTimer); clearTimeout(noticeTimer);
  window.removeEventListener("resize", keepPersonCardInView);
  document.removeEventListener("keydown", onKeydown);
  document.removeEventListener("pointerdown", onGlobalPointerDown);
  document.removeEventListener('ledger-close-details', closeFloatingCards);
});
</script>

<template>
  <div v-if="loading" class="loading-screen">正在打开往来有礼…</div>
  <template v-else-if="!account || !account.profile_completed">
    <div class="theme-auth-controls"><ThemeControl /></div>
    <AuthPage :state="authState" :actions="authActions" :maintenance-available="maintenanceAvailable" />
  </template>
        <div v-else class="app-shell">
    <aside class="sidebar" :inert="sensitiveOpen || (floatingCardOpen && !floatingPhoneView)">
      <div class="brand">
        <img class="brand-mark brand-mark-image" src="/brand-mark.png" alt="" /><strong>往来有礼</strong>
      </div>
      <nav aria-label="主导航">
        <button
          v-for="tab in tabs"
          :key="tab.id"
          type="button"
          :class="['nav-item', { active: page === tab.id }]"
          :aria-label="tab.label"
          :title="tab.label"
          @click="navigate(tab.id)"
        >
          <span class="nav-graphic"><small v-if="tab.step">{{tab.step}}</small><UiIcon class="nav-icon" :name="tab.id" /></span>
          <span class="nav-label"
          >{{ tab.label }}</span>
        </button>
      </nav>
      <div class="sidebar-user">
        <span class="avatar">{{ profile?.name?.slice(0, 1) }}</span
        ><span>{{ profile?.name }}<small>我的家庭账本</small></span>
      </div>
    </aside>
    <div class="main-area" :inert="floatingCardOpen || sensitiveOpen">
      <div class="content">
        <div class="theme-page-controls"><ThemeControl /></div>
        <p v-if="error && page !== 'me' && page !== 'stats' && !contactFormOpen && !eventFormOpen && !entryOpen && !giftEditOpen" class="alert error" role="alert">
          {{ error }}<button type="button" @click="error = ''">×</button>
        </p>
        <p v-if="notice && page !== 'me'" class="alert success" role="status">
          {{ notice }}<button type="button" @click="notice = ''">×</button>
        </p>

        <section v-if="page === 'people'">
          <div class="page-heading">
            <div>
              <div class="business-title"><h1>联系人</h1><button class="text-button guide-entry" @click="guideOpen = true">记账指南</button></div>
              <p>先登记往来的人，后续办事和记账可以重复使用</p>
            </div>
          </div>
          <RelatedViewBanner v-if="relatedView" :title="relatedView.title" :source-label="relatedView.sourceLabel" :items="relatedConditions" :count="relatedCount" :unit="relatedUnit" :loading="page==='people'?peopleLoading:page==='events'?eventsLoading:ledgerLoading" @exit="pageNavigation.exit" @back="pageNavigation.back" @remove="removeRelatedCondition" />
          <ListWorkspace v-model:page="peoplePage" v-model:page-size="peoplePageSize" title="联系人列表" :total="filteredPeople.length" unit="人" :loading="peopleLoading" :blocked="listOverlayOpen">
            <template #search>
          <SearchToolbar modal-filters :result-count="filteredPeople.length" result-unit="人" :loading="peopleLoading" :error="peopleQueryError" :validation-error="dateFilterError(peopleFilters)" @clear-filters="clearFilters('people')" v-model="peopleSearch" v-model:filter-open="peopleFilterOpen" :filters="peopleFilterChips" @remove-filter="removeFilterChip(peopleFilters, $event)" placeholder="搜索姓名或联系人备注" scope="搜索目标：姓名、联系人备注；用空格分隔关键词，匹配其中任意一个" @search="searchPeople" @clear="clearSearch('people')">
              <MultiSelect
                v-model="peopleFilters.relations"
                label="关系类型"
                :options="relationshipOptions"
                all-label="全部关系"
              />
              <MultiSelect
                v-model="peopleFilters.statuses"
                label="状态"
                :options="[
                  { value: 'active', label: '启用中' },
                  { value: 'inactive', label: '已停用' },
                ]"
                all-label="全部状态"
              />
              <MultiSelect
                v-model="peopleFilters.flows"
                label="来往状态"
                :options="[
                  { value: 'self_unreturned', label: '我未回礼' },
                  { value: 'counterparty_unreturned', label: '对方未回礼' },
                  { value: 'mutual', label: '礼尚往来' },
                  { value: 'none', label: '暂无往来' },
                ]"
                all-label="全部状态"
              />
              <div class="field date-field">
                <span class="field-label">添加日期区间</span>
                <div>
                  <input v-model="peopleFilters.date_from" type="date" /><span
                    >至</span
                  ><input v-model="peopleFilters.date_to" type="date" />
                </div>
              </div>
          </SearchToolbar>
            </template>
            <template #add><button class="primary" @click="openContact()">＋ 添加联系人</button></template>
            <template #sort><SortSelect v-model="peopleSort.field" v-model:direction="peopleSort.direction" :options="peopleSortOptions" @update:model-value="peoplePage = 1" @update:direction="peoplePage = 1" /></template>
          <section class="list-section" :aria-busy="peopleLoading">

            <div class="table-scroller" :inert="page === 'people' ? peopleLoading : page === 'events' ? eventsLoading : ledgerLoading">
              <table class="data-table people-table">
                <thead>
                  <tr>
                    <th>
                      <button class="sort-head" @click="sortPeople('name')">
                        姓名 {{ peopleSortArrow("name") }}
                      </button>
                    </th>
                    <th>
                      <button
                        class="sort-head"
                        @click="sortPeople('relationship')"
                      >
                        关系 {{ peopleSortArrow("relationship") }}
                      </button>
                    </th>
                    <th>
                      <button class="sort-head" @click="sortPeople('remark')">
                        联系人备注 {{ peopleSortArrow("remark") }}
                      </button>
                    </th>
                    <th>
                      <button class="sort-head" @click="sortPeople('flow')">
                        来往状态 {{ peopleSortArrow("flow") }}
                      </button>
                    </th>
                    <th>
                      <button class="sort-head" @click="sortPeople('expense')">
                        我随出 {{ peopleSortArrow("expense") }}
                      </button>
                    </th>
                    <th>
                      <button class="sort-head" @click="sortPeople('income')">
                        我收到 {{ peopleSortArrow("income") }}
                      </button>
                    </th>
                    <th>
                      <button class="sort-head" @click="sortPeople('balance')">
                        差额 {{ peopleSortArrow("balance") }}
                      </button>
                    </th>
                    <th>
                      <button
                        class="sort-head"
                        @click="sortPeople('created_at')"
                      >
                        添加日期 {{ peopleSortArrow("created_at") }}
                      </button>
                    </th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="person in visiblePeople"
                    :key="person.id"
                    :data-person-id="person.id"
                    tabindex="0" :class="{selected: selectedPerson?.id === person.id}" @keydown.enter.self.prevent="togglePerson(person,$event)"
                    @click="togglePerson(person, $event)"
                  >
                    <td>
                      <strong>{{ person.name }}</strong
                      ><span
                        class="status-pill"
                        :class="person.is_active ? 'enabled' : ''"
                        >{{ person.is_active ? "启用中" : "已停用" }}</span
                      >
                    </td>
                    <td>{{ person.relationship_type_name }}</td>
                    <td>{{ person.remark || "—" }}</td>
                    <td>
                      {{
                        balanceById.get(person.id)?.interaction_status?.label ||
                        "暂无往来"
                      }}
                    </td>
                    <td class="expense">
                      {{
                        balanceById.get(person.id)
                          ? money(balanceById.get(person.id).expense_amount)
                          : "—"
                      }}
                    </td>
                    <td class="income">
                      {{
                        balanceById.get(person.id)
                          ? money(balanceById.get(person.id).income_amount)
                          : "—"
                      }}
                    </td>
                    <td
                      :class="
                        balanceClass(
                          balanceById.get(person.id)?.balance?.amount,
                        )
                      "
                    >
                      {{
                        balanceById.get(person.id)
                          ? signedMoney(
                              balanceById.get(person.id).balance.amount,
                            )
                          : "—"
                      }}
                    </td>
                    <td>{{ person.created_at?.slice(0, 10) || "—" }}</td>
                    <td class="row-menu-col"><RowMenu label="联系人操作" :items="personActions(person)" @select="personAction($event,person)" /></td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div class="mobile-list" :inert="page === 'people' ? peopleLoading : page === 'events' ? eventsLoading : ledgerLoading">
              <div role="button" tabindex="0" @keydown.enter.self.prevent="togglePerson(person,$event)"
                v-for="person in visiblePeople"
                :key="person.id"
                class="mobile-card person-mobile-card" :class="{selected:selectedPerson?.id === person.id}"
                :data-person-id="person.id"
                @click="togglePerson(person, $event)"
              >
                <RowMenu label="联系人操作" :items="personActions(person)" @select="personAction($event,person)" /><span class="person-card-head"
                  ><strong>{{ person.name }}</strong
                  ><span
                    class="status-pill"
                    :class="person.is_active ? 'enabled' : ''"
                    >{{ person.is_active ? "启" : "停" }}</span
                  ><span
                    >{{ person.relationship_type_name }} ·
                    {{ person.remark || "无备注" }}</span
                  ><time>{{ person.created_at?.slice(0, 10) }}</time></span
                >
                <span class="person-card-metrics"
                  ><span>{{
                    balanceById.get(person.id)?.interaction_status?.label ||
                    "暂无往来"
                  }}</span
                  ><span
                    >我随出
                    <b class="expense">{{
                      balanceById.get(person.id)
                        ? money(balanceById.get(person.id).expense_amount)
                        : "—"
                    }}</b></span
                  ><span
                    >我收到
                    <b class="income">{{
                      balanceById.get(person.id)
                        ? money(balanceById.get(person.id).income_amount)
                        : "—"
                    }}</b></span
                  ><span
                    >差额
                    <b
                      :class="
                        balanceClass(
                          balanceById.get(person.id)?.balance?.amount,
                        )
                      "
                      >{{
                        balanceById.get(person.id)
                          ? signedMoney(
                              balanceById.get(person.id).balance.amount,
                            )
                          : "—"
                      }}</b
                    ></span
                  ></span
                >
              </div>
            </div>
            <p v-if="!peopleLoading && !visiblePeople.length" class="empty">
              没有符合条件的联系人
            </p>
          </section>
          </ListWorkspace>
        </section>

        <section v-else-if="page === 'events'">
          <div class="page-heading">
            <div>
              <div class="business-title"><h1>事件</h1><button class="text-button guide-entry" @click="guideOpen = true">记账指南</button></div>
              <p>记录谁家办什么事、哪天办，比如婚礼、乔迁、寿宴</p>
            </div>
          </div>
          <RelatedViewBanner v-if="relatedView" :title="relatedView.title" :source-label="relatedView.sourceLabel" :items="relatedConditions" :count="relatedCount" :unit="relatedUnit" :loading="page==='people'?peopleLoading:page==='events'?eventsLoading:ledgerLoading" @exit="pageNavigation.exit" @back="pageNavigation.back" @remove="removeRelatedCondition" />
          <ListWorkspace v-model:page="eventPage" v-model:page-size="eventPageSize" title="事件列表" :total="eventsTotal" unit="个事件" :loading="eventsLoading" :blocked="listOverlayOpen" @update:page="loadEvents()" @update:page-size="searchEvents()">
            <template #search>
          <SearchToolbar modal-filters :result-count="eventsTotal" result-unit="个事件" :loading="eventsLoading" :error="eventQueryError" :validation-error="dateFilterError(eventFilters)" @clear-filters="clearFilters('events')" v-model="eventSearch" v-model:filter-open="eventFilterOpen" :filters="eventFilterChips" @remove-filter="removeFilterChip(eventFilters, $event)" placeholder="搜索事主或事件备注" scope="搜索目标：事主姓名、联系人备注、事件备注；用空格分隔关键词，匹配其中任意一个" @search="searchEvents" @clear="clearSearch('events')">
              <MultiSelect
                v-model="eventFilters.relations"
                label="事主与我的关系类型"
                :options="relationshipOptions"
                all-label="全部关系"
              />
              <MultiSelect
                v-model="eventFilters.kinships"
                label="亲属称谓"
                :options="kinshipOptions"
                all-label="全部称谓"
              />
              <MultiSelect
                v-model="eventFilters.types"
                label="事件类型"
                :options="eventTypeOptions"
                all-label="全部事件"
              />
              <div class="field date-field">
                <span class="field-label">办事日期区间</span>
                <div>
                  <input v-model="eventFilters.date_from" type="date" /><span
                    >至</span
                  ><input v-model="eventFilters.date_to" type="date" />
                </div>
              </div>
          </SearchToolbar>
            </template>
            <template #add><button class="primary" @click="openEventForm()">＋ 添加事件</button></template>
            <template #sort><SortSelect v-model="eventSort.sort_by" v-model:direction="eventSort.sort_order" :options="eventSortOptions" @update:model-value="searchEvents()" @update:direction="searchEvents()" /></template>
          <section class="list-section" :aria-busy="eventsLoading">


            <div class="table-scroller" :inert="page === 'people' ? peopleLoading : page === 'events' ? eventsLoading : ledgerLoading">
              <table class="data-table events-table"><thead><tr>
                <th v-for="[field,label] in [['host_name','事主事件'],['event_date','办事日期'],['registered_at','登记日期'],['gift_count','记账记录'],['cash_amount','礼金合计']]" :key="field"><button class="sort-head" @click="sortRemote('events',field)">{{label}} {{sortArrow(eventSort,field)}}</button></th><th class="row-menu-col"><span class="sr-only">操作</span></th>
              </tr></thead><tbody>
                <tr v-for="row in events" :key="row.event.id" tabindex="0" :class="{selected:selectedEvent?.event.id === row.event.id}" @click="toggleEvent(row,$event)" @keydown.enter.self.prevent="toggleEvent(row,$event)">
                  <td class="event-cell"><strong :title="eventTitle(row)">{{eventTitle(row)}}</strong></td>
                  <td>{{eventDate(row)}}</td><td>{{row.event.created_at?.slice(0,10) || '—'}}</td><td class="number-col">{{row.gift_count}} 笔</td><td class="amount-col">{{money(row.cash_amount)}}</td>
                  <td class="row-menu-col"><RowMenu label="事件操作" :items="[{id:'edit',label:'修改事件'},{id:'delete',label:'删除事件',danger:true}]" @select="eventAction($event,row)" /></td>
                </tr>
              </tbody></table>
            </div>
            <div class="mobile-list" :inert="page === 'people' ? peopleLoading : page === 'events' ? eventsLoading : ledgerLoading">
              <div v-for="row in events" :key="row.event.id" class="mobile-card event-mobile-card" role="button" tabindex="0" :class="{selected:selectedEvent?.event.id === row.event.id}" @click="toggleEvent(row,$event)" @keydown.enter.self.prevent="toggleEvent(row,$event)">
                <RowMenu label="事件操作" :items="[{id:'edit',label:'修改事件'},{id:'delete',label:'删除事件',danger:true}]" @select="eventAction($event,row)" />
                <strong class="card-title" :title="eventTitle(row)">{{eventTitle(row)}}</strong>
                <div class="card-dates"><span>办事日期 {{eventDate(row)}}</span><span>登记日期 {{row.event.created_at?.slice(0,10) || '—'}}</span></div>
                <div class="card-dates"><span>记账记录 {{row.gift_count}} 笔</span><span>礼金合计 <b>{{money(row.cash_amount)}}</b></span></div>
              </div>
            </div>
            <p v-if="eventsLoading" class="loading-state" role="status">正在加载事件…</p>
            <p v-if="!eventsLoading && !events.length" class="empty">暂无符合条件的事件</p>
          </section>
          </ListWorkspace>
        </section>

        <section v-else-if="page === 'ledger'">
          <div class="page-heading">
            <div>
              <div class="business-title"><h1>记账</h1><button class="text-button guide-entry" @click="guideOpen = true">记账指南</button></div>
              <p>选择已有事件，再登记礼金或礼物</p>
            </div>
          </div>
          <RelatedViewBanner v-if="relatedView" :title="relatedView.title" :source-label="relatedView.sourceLabel" :items="relatedConditions" :count="relatedCount" :unit="relatedUnit" :loading="page==='people'?peopleLoading:page==='events'?eventsLoading:ledgerLoading" @exit="pageNavigation.exit" @back="pageNavigation.back" @remove="removeRelatedCondition" />
          <ListWorkspace v-model:page="ledgerPage" v-model:page-size="ledgerPageSize" title="记账列表" :total="ledger.page.total" unit="笔" :loading="ledgerLoading" :blocked="listOverlayOpen" @update:page="loadLedger()" @update:page-size="searchLedger()">
            <template #search>
          <SearchToolbar modal-filters :result-count="ledger.page.total" result-unit="笔" :loading="ledgerLoading" :error="ledgerQueryError" :validation-error="dateFilterError(ledgerFilters)" @clear-filters="clearFilters('ledger')" v-model="ledgerSearch" v-model:filter-open="ledgerFilterOpen" :filters="ledgerFilterChips" @remove-filter="removeFilterChip(ledgerFilters, $event)" placeholder="搜索事主或随礼人" :scope="GIFT_SEARCH_SCOPE" @search="searchLedger" @clear="clearSearch('ledger')">
              <MultiSelect
                v-model="ledgerFilters.relations"
                label="事主与我的关系类型"
                :options="relationshipOptions"
                all-label="全部关系"
              />
              <MultiSelect
                v-model="ledgerFilters.kinships"
                label="亲属称谓"
                :options="kinshipOptions"
                all-label="全部称谓"
              />
              <MultiSelect
                v-model="ledgerFilters.types"
                label="事件类型"
                :options="eventTypeOptions"
                all-label="全部事件"
              />
              <div class="field date-field">
                <span class="field-label">{{ledgerContext?.params?.date_basis==='registered'?'登记日期区间':'办事日期区间'}}</span>
                <div>
                  <input v-model="ledgerFilters.date_from" type="date" /><span
                    >至</span
                  ><input v-model="ledgerFilters.date_to" type="date" />
                </div>
              </div>
          </SearchToolbar>
            </template>
            <template #add><button class="primary" @click="openEntry()">＋ 记一笔账</button></template>
            <template #sort><SortSelect v-model="ledgerSort.sort_by" v-model:direction="ledgerSort.sort_order" :options="ledgerSortOptions" @update:model-value="searchLedger()" @update:direction="searchLedger()" /></template>

          <section class="list-section" :aria-busy="ledgerLoading">

            <div class="table-scroller" :inert="page === 'people' ? peopleLoading : page === 'events' ? eventsLoading : ledgerLoading">
              <table class="data-table ledger-table">
                <thead>
                  <tr>
                    <th><button class="sort-head" @click="sortRemote('ledger','host_name')">事主事件 {{sortArrow(ledgerSort,'host_name')}}</button></th>
                    <th><button class="sort-head" @click="sortRemote('ledger','giver_name')">随礼人 {{sortArrow(ledgerSort,'giver_name')}}</button></th>
                    <th class="amount-col"><button class="sort-head" @click="sortRemote('ledger','amount')">礼金 {{sortArrow(ledgerSort,'amount')}}</button></th>
                    <th>礼物</th>
                    <th><button class="sort-head" @click="sortRemote('ledger','event_date')">办事日期 {{sortArrow(ledgerSort,'event_date')}}</button></th>
                    <th><button class="sort-head" @click="sortRemote('ledger','gift_created_at')">登记日期 {{sortArrow(ledgerSort,'gift_created_at')}}</button></th>
                    <th>备注</th>
                    <th class="row-menu-col"><span class="sr-only">操作</span></th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="gift in ledger.items"
                    :key="gift.id"
                    class="ledger-row" :class="{selected:ledgerGift?.id === gift.id}"
                    tabindex="0"
                    @click="toggleLedgerGift(gift, $event)"
                    @keydown.enter.self.prevent="toggleLedgerGift(gift, $event)"
                  >
                    <td class="event-cell"><strong>{{ eventTitle(gift.event) }}</strong></td>
                    <td class="giver-cell">
                      {{ queryContactLabel(gift.giver) }}
                    </td>
                    <td class="amount-col">
                      <span :class="giftClass(gift.direction)"
                        >{{ giftMark(gift.direction) }}{{ money(gift.amount) }}</span
                      >
                    </td>
                    <td class="gift-cell">
                      <span
                        class="gift-text"
                        tabindex="0"
                        :aria-label="`礼物明细：${gift.gift_details || '无'}`"
                        @mouseenter="showGiftTip(gift.gift_details, $event)"
                        @mouseleave="hideGiftTip"
                        @focus="showGiftTip(gift.gift_details, $event)"
                        @blur="hideGiftTip"
                        >{{ gift.gift_details || "—" }}</span
                      >
                    </td>
                    <td>{{ eventDate(gift) }}</td>
                    <td>{{ gift.created_at?.slice(0, 10) || "—" }}</td>
                    <td class="remark-cell">
                      <span :title="gift.remark || ''">{{
                        gift.remark || "—"
                      }}</span>
                    </td>
                    <td class="row-menu-col">
                      <RowMenu label="记账记录操作" :items="[{id:'edit',label:'修改记账记录'},{id:'delete',label:'删除记账记录',danger:true}]" @select="giftAction($event,gift)" />
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div class="mobile-list" :inert="page === 'people' ? peopleLoading : page === 'events' ? eventsLoading : ledgerLoading">
              <div v-for="gift in ledger.items" :key="gift.id" class="mobile-card ledger-mobile-card" role="button" tabindex="0" :class="{selected:ledgerGift?.id === gift.id}" @click="toggleLedgerGift(gift,$event)" @keydown.enter.self.prevent="toggleLedgerGift(gift,$event)">
                <RowMenu label="记账记录操作" :items="[{id:'edit',label:'修改记账记录'},{id:'delete',label:'删除记账记录',danger:true}]" @select="giftAction($event,gift)" />
                <strong class="card-title" :title="eventTitle(gift.event)">{{eventTitle(gift.event)}}</strong>
                <div class="card-dates"><span>办事日期 {{eventDate(gift)}}</span><span>登记日期 {{gift.created_at?.slice(0,10) || '—'}}</span></div>
                <div class="card-dates"><span class="giver-name">随礼人 {{queryContactLabel(gift.giver)}}</span><b :class="giftClass(gift.direction)">{{giftMark(gift.direction)}}{{money(gift.amount)}}</b></div>
                <div class="ledger-card-tail"><small>礼物：{{gift.gift_details || '—'}}</small><small>备注：{{gift.remark || '—'}}</small></div>
              </div>
            </div>
            <p v-if="ledgerLoading" class="loading-state" role="status">正在加载记账记录…</p><p v-if="!ledgerLoading && !ledger.items.length" class="empty">暂无符合条件的记账记录</p>
          </section>
          </ListWorkspace>
        </section>

        <StatsPage :key="pageRouteKey" v-else-if="page === 'stats'" :contacts="contacts" :dictionaries="dictionaries" :view-state="statisticsView" @update:view-state="saveStatisticsView" @drill="openLedgerForStatistics" />
        <MePage v-else-if="page === 'me'" :state="meState" :actions="meActions" :maintenance-available="maintenanceAvailable" />
              </div>
    </div>

    <nav class="bottom-nav" aria-label="手机主导航" :inert="sensitiveOpen || (floatingCardOpen && !floatingPhoneView)">
      <button
        v-for="tab in tabs"
        :key="tab.id"
        :class="{ active: page === tab.id }"
        :aria-label="tab.label"
        @click="navigate(tab.id)"
      >
        <span class="nav-graphic"><small v-if="tab.step">{{tab.step}}</small><UiIcon :name="tab.id" /></span>{{ tab.label }}
      </button>
    </nav>

    <BusinessGuide v-if="guideOpen" :page="page" @close="guideOpen=false" @navigate="guideAction" />

    <DetailCard
      v-if="selectedPerson && page === 'people'"
      title="联系人详情" close-label="关闭联系人卡片" :phone="floatingPhoneView"
      :set-card-ref="personCard.setRef" :position="personCard.position"
      @close="dismissFloatingCards" @drag-start="personCard.startDrag" @drag-move="personCard.moveDrag" @drag-end="personCard.stopDrag"
    >
        <div class="detail-head">
          <h2>
            {{ summaryName(selectedPerson)
            }}<span
              class="status-pill"
              :class="selectedPerson.is_active ? 'enabled' : ''"
              >{{ selectedPerson.is_active ? "启用中" : "已停用" }}</span
            >
          </h2>
        </div>
        <div class="card-metrics">
          <span class="metric"><small>来往状态</small><b>{{personBalance(selectedPerson.id)?.interaction_status?.label || '暂无往来'}}</b></span>
          <span class="metric family-money"><small>我随出</small><b class="expense">{{personBalance(selectedPerson.id) ? money(personBalance(selectedPerson.id).expense_amount) : '—'}}</b></span>
          <span class="metric family-money"><small>我收到</small><b class="income">{{personBalance(selectedPerson.id) ? money(personBalance(selectedPerson.id).income_amount) : '—'}}</b></span>
          <span class="metric family-money"><small>差额</small><b :class="balanceClass(personBalance(selectedPerson.id)?.balance?.amount)">{{personBalance(selectedPerson.id) ? signedMoney(personBalance(selectedPerson.id).balance.amount) : '—'}}</b></span>
          <span class="metric"><small>电话</small><b>{{selectedPerson.phone || '—'}}</b></span>
          <span class="metric"><small>添加日期</small><b>{{selectedPerson.created_at?.slice(0,10) || '—'}}</b></span>
        </div>
        <div class="approved-detail-actions contact-business-actions">
          <button class="soft-primary" :disabled="busy || !selectedPerson.is_active" :title="!selectedPerson.is_active ? '请先启用此联系人' : ''" @click="openEventForPerson(selectedPerson)">添加此人事件</button>
          <button class="soft-primary" data-detail-scope="host-events" :data-availability="detailAvailabilityState('events', selectedPerson).status" :disabled="detailLinkDisabled('events', selectedPerson)" :title="detailLinkTitle('events', selectedPerson)" @click="openEventsForContact(selectedPerson)">查看此人事件</button>
          <button class="soft-primary" data-detail-scope="contact-gifts" :data-availability="detailAvailabilityState('gifts', selectedPerson).status" :disabled="detailLinkDisabled('gifts', selectedPerson)" :title="detailLinkTitle('gifts', selectedPerson)" @click="openLedgerForContact(selectedPerson)">查看此人记账</button>
        </div>
        <p v-if="detailChecksFailed || detailChecksLoading" class="muted detail-availability-status" role="status">
          {{ detailChecksFailed ? '可查看资料的数量暂时检查失败。' : '正在核对可查看的资料…' }}
          <button v-if="detailChecksFailed" type="button" class="small-link" @click="retryDetailAvailability">重试</button>
        </p>
        <div class="approved-detail-actions">
          <div class="split-actions"><button class="soft-primary" :disabled="selectedPerson.is_self || selectedPerson.is_active || busy" @click="setContactActive(selectedPerson)">启用</button><button class="soft-primary" :disabled="selectedPerson.is_self || !selectedPerson.is_active || busy" @click="setContactActive(selectedPerson)">停用</button></div>
          <div class="split-actions"><button class="soft-primary" :disabled="busy" @click="openContact(selectedPerson)">修改</button><button class="soft-primary" :disabled="selectedPerson.is_self || selectedPerson.has_references || busy" @click="deleteContact(selectedPerson)">删除</button></div>
        </div>
    </DetailCard>

    <DetailCard
      v-if="selectedEvent && page === 'events'"
      title="事件详情" close-label="关闭事件卡片" :phone="floatingPhoneView" card-class="event-float-card"
      :set-card-ref="eventCard.setRef" :position="eventCard.position"
      @close="dismissFloatingCards" @drag-start="eventCard.startDrag" @drag-move="eventCard.moveDrag" @drag-end="eventCard.stopDrag"
    >
        <div class="detail-head">
          <h2>{{hostLabel(selectedEvent)}}<span class="status-pill">{{selectedEvent.event.kinship_type.name}}</span></h2>
        </div>
        <div class="card-metrics">
          <span class="metric"><small>事件类型</small><b>{{selectedEvent.event.event_type.name}}</b></span>
          <span class="metric"><small>办事日期</small><b>{{eventDate(selectedEvent)}}</b></span>
          <span class="metric"><small>登记日期</small><b>{{selectedEvent.event.created_at?.slice(0,10) || '—'}}</b></span>
          <span class="metric"><small>记账笔数</small><b>{{selectedEvent.gifts.length}} 笔</b></span>
          <span class="metric family-money"><small>礼金合计</small><b>{{money(selectedEvent.gifts.reduce((sum,gift) => sum + Number(gift.amount),0))}}</b></span>
          <span class="metric"><small>备注</small><b>{{selectedEvent.event.remark || '—'}}</b></span>
        </div>
        <div class="approved-detail-actions">
          <button class="soft-primary" :disabled="eventHasMyGift(selectedEvent) || busy" @click="addGiftToEvent(selectedEvent.event.id)">为此事件记一笔账</button>
          <button class="soft-primary" :disabled="eventHostIsSelf(selectedEvent)" @click="openPersonCard(selectedEvent.event.host.id)">查看事主汇总信息</button>
          <button class="soft-primary" data-detail-scope="event-gifts" :data-availability="detailAvailabilityState('eventGifts', selectedEvent.event).status" :disabled="detailLinkDisabled('eventGifts', selectedEvent.event)" :title="detailLinkTitle('eventGifts', selectedEvent.event)" @click="openLedgerForEvent(selectedEvent.event.id,eventTitle(selectedEvent.event))">看此事件全部记账</button>
          <div class="split-actions"><button class="soft-primary" :disabled="busy" @click="openEventForm(selectedEvent)">修改</button><button class="soft-primary" :disabled="busy || selectedEvent.gifts.length > 0" @click="deleteEvent(selectedEvent)">删除</button></div>
        </div>
        <p v-if="detailChecksFailed || detailChecksLoading" class="muted detail-availability-status" role="status">
          {{ detailChecksFailed ? '可查看资料的数量暂时检查失败。' : '正在核对可查看的资料…' }}
          <button v-if="detailChecksFailed" type="button" class="small-link" @click="retryDetailAvailability">重试</button>
        </p>
    </DetailCard>

    <DetailCard
      v-if="ledgerGift && page === 'ledger'"
      title="记账详情" close-label="关闭记账卡片" :phone="floatingPhoneView"
      :set-card-ref="giftCard.setRef" :position="giftCard.position"
      @close="dismissFloatingCards" @drag-start="giftCard.startDrag" @drag-move="giftCard.moveDrag" @drag-end="giftCard.stopDrag"
    >
        <div class="detail-head">
          <h2>{{eventTitle(ledgerGift.event)}}</h2>
        </div>
        <div class="card-metrics">
          <span class="metric"><small>随礼人</small><b>{{queryContactLabel(ledgerGift.giver)}}</b></span>
          <span class="metric family-money"><small>礼金</small><b :class="giftClass(ledgerGift.direction)">{{giftMark(ledgerGift.direction)}}{{money(ledgerGift.amount)}}</b></span>
          <span class="metric"><small>礼物</small><b>{{ledgerGift.gift_details || '—'}}</b></span>
          <span class="metric"><small>办事日期</small><b>{{eventDate(ledgerGift)}}</b></span>
          <span class="metric"><small>登记日期</small><b>{{ledgerGift.created_at?.slice(0,10) || '—'}}</b></span>
          <span class="metric"><small>备注</small><b>{{ledgerGift.remark || '—'}}</b></span>
        </div>
        <div class="approved-detail-actions">
          <button class="soft-primary" @click="openPersonCard(ledgerGift.counterparty?.id)">查看关系人信息汇总</button>
          <button class="soft-primary" data-detail-scope="host-events" :data-availability="detailAvailabilityState('events', ledgerGift.counterparty).status" :disabled="detailLinkDisabled('events', ledgerGift.counterparty)" :title="detailLinkTitle('events', ledgerGift.counterparty)" @click="openEventsForContact(ledgerGift.counterparty)">查看关系人事件记录</button>
          <button class="soft-primary" :disabled="busy" @click="openGiftEditor(ledgerGift)">修改本条记账</button>
          <button class="soft-primary" :disabled="busy" @click="deleteGift(ledgerGift)">删除本条记账</button>
        </div>
        <p v-if="detailChecksFailed || detailChecksLoading" class="muted detail-availability-status" role="status">
          {{ detailChecksFailed ? '可查看资料的数量暂时检查失败。' : '正在核对可查看的资料…' }}
          <button v-if="detailChecksFailed" type="button" class="small-link" @click="retryDetailAvailability">重试</button>
        </p>
    </DetailCard>

    <ModalShell v-if="contactFormOpen" family :title="contactEditingId ? '修改联系人' : '添加联系人'"  :closing-disabled="busy" @close="closeForm('contact')">
        <form id="contact-form" class="form-stack" autocomplete="off" :inert="busy" @submit.prevent="saveContact">
          <label class="field"
            >姓名 *<input
              v-model="contactForm.name"
              required
              maxlength="50" /></label
          ><label class="field"
            >关系类型 *<select
              v-model="contactForm.relationship_type_id"
              required
            >
              <option :value="null">请选择关系</option>
              <option
                v-for="x in dictionaries.relationship.filter(
                  (x) => x.is_active && x.name !== '我',
                )"
                :key="x.id"
                :value="x.id"
              >
                {{ x.name }}
              </option>
            </select></label
          ><button
            type="button"
            class="small-link"
            @click="addDictionary('relationship')"
          >
            ＋ 自定义关系类型</button
          ><label class="field"
            >电话<input v-model="contactForm.phone" type="tel" inputmode="tel" autocomplete="off" placeholder="选填" maxlength="20" /></label
          ><label class="field"
            >联系人备注<input
              v-model="contactForm.remark"
              placeholder="同名联系人请填写不同备注"
          /></label>
          <div v-if="sameNameContacts.length" class="same-name-list">
            <p>同名联系人（请用备注区分）：</p>
            <div v-for="match in sameNameContacts" :key="match.id">
              <strong>{{ match.name }}</strong>
              <span
                class="status-pill"
                :class="match.is_active ? 'enabled' : ''"
                >{{ match.is_active ? "启用中" : "已停用" }}</span
              >
              <span
                v-if="
                  identityPart(match.remark) ===
                  identityPart(contactForm.remark)
                "
                class="expense"
                >已有两项相同</span
              >
              <small
                >{{ match.relationship_type_name }} ·
                {{ match.remark || "无备注" }}</small
              >
              <button
                type="button"
                class="text-button"
                @click="viewSameName(match,$event)"
              >
                查看
              </button>
            </div>
          </div>
          <p v-if="duplicateContact" class="alert error">
            已有姓名和备注相同的联系人，请填写不同备注。
          </p>

        </form><p v-if="error" class="alert error" role="alert">{{error}}</p>
      <template #footer><div class="form-actions">
            <button
              type="button"
              class="outline"
              :disabled="busy" @click="closeForm('contact')"
            >
              取消</button
            ><button form="contact-form" class="primary" :disabled="busy || !!duplicateContact">
              保存
            </button>
          </div></template>
    </ModalShell>
    <ModalShell v-if="eventFormOpen" family :title="eventEditingId ? '修改事件' : '添加事件'"  :closing-disabled="busy" @close="closeForm('event')">
      <p v-if="eventDraftReturn" class="muted draft-return-note">正在修改原事件。原来的填写已保留。<button type="button" class="text-button" @click="returnToEventDraft()">返回原填写</button></p>
        <form id="event-form" class="form-stack" autocomplete="off" :inert="busy" @submit.prevent="saveEvent">
          <div class="field">
            <span class="field-label">事主 *</span
            ><ContactPicker
              v-model="eventForm.host_contact_id"
              :contacts="
                contacts.filter(
                  (x) => x.is_active || x.id === eventForm.host_contact_id,
                )
              "
              self-first
            />
          </div>
          <div class="form-grid">
            <label class="field"
              >亲属称谓 *<select v-model="eventForm.kinship_type_id" required>
                <option :value="null">请选择</option>
                <option
                  v-for="x in dictionaries.kinship.filter((x) => x.is_active)"
                  :key="x.id"
                  :value="x.id"
                >
                  {{ x.name }}
                </option></select
              ><button
                type="button"
                class="small-link"
                @click.prevent="addDictionary('kinship')"
              >
                ＋ 自定义
              </button></label
            ><label class="field"
              >事件类型 *<select v-model="eventForm.event_type_id" required>
                <option :value="null">请选择</option>
                <option
                  v-for="x in dictionaries.event.filter((x) => x.is_active)"
                  :key="x.id"
                  :value="x.id"
                >
                  {{ x.name }}
                </option></select
              ><button
                type="button"
                class="small-link"
                @click.prevent="addDictionary('event')"
              >
                ＋ 自定义
              </button></label
            >
          </div>
          <label class="field"
            >办事日期 *<input
              v-model="eventForm.event_date"
              type="date"
              min="1000-01-01" max="9999-12-31"
              :required="!eventEditingId || isCompleteEventDate(eventEditingSource)" />
              <small v-if="eventEditingId && !isCompleteEventDate(eventEditingSource)" class="field-help">原旧账日期：{{ eventDate(eventEditingSource) }}。留空保持原日期；补齐时保留已知年、月，已有记账的统计日期会一并更新。</small></label
          ><label class="field"
            >事件备注<RemarkTextarea
              v-model="eventForm.remark"
              placeholder="选填"
            />
            <small class="field-help">多次发生的同类事件，请用备注区分。</small>
          </label>
          <p v-if="eventIdentityChecking" class="muted field-help" role="status">正在核对已有事件…</p><p v-else-if="eventIdentityError" class="muted field-help" role="status">{{ eventIdentityError }}</p>
          <section v-if="duplicateEvent" class="conflict-card event-conflict" role="status" aria-label="已有相同事件">
            <p class="conflict-message">这个事件已经登记，请查看或修改原事件。</p>
            <div class="conflict-summary"><strong>{{ eventTitle(duplicateEvent) }}</strong><span>备注：{{ duplicateEvent.event.remark || '无备注' }}</span><span>办事日期：{{ eventDate(duplicateEvent) }}</span></div>
            <div class="conflict-actions"><button type="button" class="outline" @click="eventConflictExpanded = !eventConflictExpanded">{{ eventConflictExpanded ? '收起详情' : '查看' }}</button><button type="button" class="soft-primary" @click="editDuplicateEvent">修改</button></div>
            <div v-if="eventConflictExpanded" class="conflict-detail"><p>登记日期：{{ stamp(duplicateEvent.event.created_at) }}</p><p>备注：{{ duplicateEvent.event.remark || '无备注' }}</p><p class="muted">事主、亲属称谓、事件类型和备注相同，日期不同也属于同一个事件。</p></div>
          </section>

        </form><p v-if="error" class="alert error" role="alert">{{error}}</p>
      <template #footer><div class="form-actions">
            <button
              type="button"
              class="outline"
              :disabled="busy" @click="closeForm('event')"
            >
              取消</button
            ><button form="event-form" class="primary" :disabled="busy || eventIdentityChecking || !!duplicateEvent">保存事件</button>
          </div></template>
    </ModalShell>
    <ModalShell v-if="entryOpen" family class="entry-notes-modal" title="记一笔" wide :closing-disabled="busy" @close="closeForm('entry')">
      <div class="entry-layout"><section class="entry-picker">
        <EntryEventPicker v-model:search="entryEventSearch" :rows="entryEventMatches" :selected-id="entryEvent?.event.id" :selected-title="entryEvent ? eventTitle(entryEvent) : ''" :selected-date="entryEvent ? eventDate(entryEvent) : ''" :loading="entryLoading" :selecting="entrySelecting" :busy="busy" :format-title="eventTitle" :format-stamp="stamp" @choose="chooseEntryEvent" />
        </section><section class="entry-form"><template v-if="entryEvent">
        <EntryEventSummary :detail="entryEvent" :format-title="eventTitle" :format-stamp="stamp" />
        <section v-if="!isCompleteEventDate(entryEvent)" class="conflict-card entry-date-completion" role="status">
          <p>这个旧账事件的办事日期为“{{ eventDate(entryEvent) }}”。网页新增记账需要完整日期，请先补齐原事件日期。</p>
          <label class="field">补齐办事日期<input v-model="entryCompleteDate" type="date" min="1000-01-01" max="9999-12-31" :disabled="busy" /></label>
          <p class="muted field-help">保留已知年、月。补齐会同时更新该事件已有记账的统计日期，填写的随礼人资料保持。</p>
          <button type="button" class="outline" :disabled="busy || !entryCompleteDate" @click="completeEntryDate">核对并补齐原日期</button>
        </section>
        <p
            class="direction-strip neutral muted"
          >
            {{
              entryIsSelf
                ? "我家办事：选择联系人随礼，系统自动记为收入。"
                : "别人办事：本人随礼，系统自动记为支出。"
            }}
          </p>
          <section v-if="entryDuplicateGift" class="conflict-card gift-conflict" role="status" aria-label="已有同一随礼人记账">
            <p class="conflict-message">这个事件已有这位随礼人的记账，请查看或修改原记账记录。</p>
            <div class="conflict-summary"><strong>{{ summaryName(entryDuplicateGift.giver) }}</strong><span>礼金：<b :class="entryIsSelf ? 'income' : 'expense'">{{ money(entryDuplicateGift.amount) }}</b></span><span>实物：{{ entryDuplicateGift.gift_details || '—' }}</span><span>登记日期：{{ stamp(entryDuplicateGift.created_at) }}</span><span>记账备注：{{ entryDuplicateGift.remark || '无备注' }}</span></div>
            <div class="conflict-actions"><button type="button" class="outline" @click="entryConflictExpanded = !entryConflictExpanded">{{ entryConflictExpanded ? '收起详情' : '查看' }}</button><button type="button" class="soft-primary" @click="editDuplicateGift">修改</button></div>
            <div v-if="entryConflictExpanded" class="conflict-detail"><p>{{ eventTitle(entryEvent) }}</p><p>办事日期：{{ eventDate(entryEvent) }}</p><p>登记日期：{{ stamp(entryDuplicateGift.created_at) }}</p><p class="muted">查看后可以继续原来的填写；修改会进入这笔原记账记录。</p></div>
          </section>
          <div
            v-for="(card, index) in entryCards"
            :key="index"
            class="giver-card"
            :class="`giver-color-${index + 1}`"
          >
            <div class="giver-heading">
              <h3>随礼人{{ index + 1 }}</h3>
              <button
                type="button"
                class="icon-button"
                :disabled="index === 0 || busy || entrySelecting"
                :aria-label="`关闭随礼人${index + 1}`"
                @click="entryCards.splice(index, 1)"
              >
                ×
              </button>
            </div>
            <div class="form-stack">
              <div class="field">
                <span class="field-label">随礼人 *</span
                ><ContactPicker
                  v-if="entryIsSelf"
                  v-model="card.giver_contact_id"
                  :contacts="entryAvailable"
                  :exclude-ids="giverExcludes(index)"
                  placeholder="输入姓名或备注筛选，或展开选择"
                /><input v-else :value="selfContact?.name || '本人'" disabled />
              </div>
              <div class="field">
                <span class="field-label">礼金金额 *</span
                ><AmountInput
                  v-model="card.amount"
                  placeholder="必填项·仅实物时填0"
                  aria-label="礼金金额"
                />
              </div>
              <label class="field"
                >实物明细<input
                  v-model="card.gift_details"
                  maxlength="255"
                  placeholder="纯实物时必填，例如一箱牛奶" /></label
              ><label class="field"
                >记账备注<RemarkTextarea
                  v-model="card.remark"
                  placeholder="选填"
                />
              </label>
            </div>
          </div>
          <p class="muted entry-footer-hint">
            同一随礼人在同一事件中只登记一笔；补充或纠错请编辑原记账记录。
          </p>
          </template><p v-else class="entry-placeholder">先从已有事件中选择，再填写随礼人和礼金。</p></section></div><p v-if="error" class="alert error" role="alert">{{error}}</p>
      <template #footer><div class="form-actions">
            <button class="outline" :disabled="busy || entrySelecting" @click="closeForm('entry')">取消</button
            ><button
              class="outline"
              :disabled="
                !entryIsSelf || entrySelecting ||
                entryCards.length >= 5 ||
                entryAvailable.length <= entryCards.length
              "
              @click="addGiverCard"
            >
              ＋ 其他随礼人</button
            ><button
              class="primary"
              :disabled="busy || entryLoading || entrySelecting || !entryEvent || entryDuplicateGift || !isCompleteEventDate(entryEvent)"
              @click="saveEntry"
            >
              保存记账记录
            </button>
          </div></template>
    </ModalShell>
    <ModalShell v-if="giftEditOpen && giftEditDetail" family title="更正记账记录"  :closing-disabled="busy" @close="closeForm('gift')">
      <p v-if="entryDraftReturn" class="muted draft-return-note">正在修改原记账记录。之前的填写已保留，保存或取消后返回。</p>
      <p class="muted">
          {{ eventTitle(giftEditDetail.event) }} · 办事日期
          {{ eventDate(giftEditDetail) }}
        </p>
        <form id="gift-form" class="form-stack" :inert="busy" @submit.prevent="saveGiftEdit">
          <div class="field">
            <span class="field-label">随礼人 *</span>
            <ContactPicker
              v-if="giftEditIsSelf"
              v-model="giftEditForm.giver_contact_id"
              :contacts="giftEditCandidates"
            />
            <input v-else :value="selfContact?.name || '本人'" disabled />
          </div>
          <div class="field">
            <span class="field-label">礼金金额 *</span
            ><AmountInput
              v-model="giftEditForm.amount"
              placeholder="必填项·仅实物时填0"
              aria-label="礼金金额"
            />
          </div>
          <label class="field"
            >实物明细<input
              v-model="giftEditForm.gift_details"
              maxlength="255"
              placeholder="纯实物时必填"
          /></label>
          <label class="field"
            >记账备注<RemarkTextarea
              v-model="giftEditForm.remark"
              placeholder="选填"
            />
          </label>

        </form><p v-if="error" class="alert error" role="alert">{{error}}</p>
      <template #footer><div class="form-actions">
            <button
              type="button"
              class="danger-outline"
              :disabled="busy"
              @click="
                deleteGift({
                  id: giftEditDetail.gift.id,
                  giver: giftEditDetail.gift.giver,
                })
              "
            >
              删除记账记录</button
            ><button
              type="button"
              class="outline"
              :disabled="busy" @click="closeForm('gift')"
            >
              取消</button
            ><button form="gift-form" class="primary" :disabled="busy">保存修改</button>
          </div></template>
    </ModalShell>
    <div
      v-if="giftTip.text"
      class="gift-tip"
      role="tooltip"
      :style="{ left: `${giftTip.left}px`, top: `${giftTip.top}px` }"
    >
      {{ giftTip.text }}
    </div>

    <ModalShell v-if="reauthOpen" family title="修改或删除记账记录前请再验证" class="reauth-layer compact-family-dialog" :closing-disabled="reauthBusy" @close="closeReauth(false)">
      <p class="muted">修改或删除记账记录前需要验证当前账号密码。验证通过后 60 分钟内有效，退出登录后立即失效。</p>
      <form id="reauth-form" class="form-stack" @submit.prevent="submitReauth">
        <label class="field">当前账号密码<input v-model="reauthPassword" type="password" autocomplete="current-password" required /></label>
        <p v-if="reauthError" class="alert error" role="alert">{{reauthError}}</p>
      </form>
      <template #footer><div class="form-actions"><button class="outline" :disabled="reauthBusy" @click="closeReauth(false)">取消</button><button class="primary" form="reauth-form" :disabled="reauthBusy || !reauthPassword">{{reauthBusy ? '正在验证…' : '验证并继续'}}</button></div></template>
    </ModalShell>
    <ModalShell v-if="sensitiveOpen" family :title="`${sensitiveAction.label}前请验证`" class="reauth-layer compact-family-dialog" :closing-disabled="sensitiveBusy" @close="closeSensitiveAuthorization()">
      <p class="muted">请验证当前账号密码。本次验证仅用于“{{sensitiveAction.label}}”，5 分钟内有效，完成一次操作后失效。</p>
      <form id="sensitive-action-form" class="form-stack" @submit.prevent="submitSensitiveAuthorization">
        <label class="field">当前账号密码<input v-model="sensitivePassword" type="password" autocomplete="current-password" :disabled="sensitiveBusy" required /></label>
        <p v-if="sensitiveError" class="alert error" role="alert">{{sensitiveError}}</p>
      </form>
      <template #footer><div class="form-actions"><button class="outline" :disabled="sensitiveBusy" @click="closeSensitiveAuthorization()">取消</button><button class="primary" form="sensitive-action-form" :disabled="sensitiveBusy || !sensitivePassword">{{sensitiveBusy ? '正在验证…' : '验证并继续'}}</button></div></template>
    </ModalShell>
    <UiDialog :state="dialog.state" @answer="dialog.answer" />
  </div>
</template>
