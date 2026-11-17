<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, useId, watch } from "vue";

const props = defineProps({
  modelValue: { type: Number, default: null },
  contacts: { type: Array, default: () => [] },
  excludeIds: { type: Array, default: () => [] },
  placeholder: { type: String, default: "输入姓名或备注筛选，或展开选择" },
  selfFirst: { type: Boolean, default: false },
});
const emit = defineEmits(["update:modelValue"]);
const root = ref(null), inputElement = ref(null), menu = ref(null);
const menuId = useId();
const open = ref(false), query = ref(""), searching = ref(false);
const selected = computed(() =>
  props.contacts.find((x) => x.id === props.modelValue),
);
// 搜索文字和已选 ID 分开：未选择的文字绝不被当成有效联系人。
const input = computed({
  get: () => searching.value ? query.value : selected.value ? label(selected.value) : "",
  set: (value) => { query.value = value; searching.value = true; },
});
watch(() => props.modelValue, (id) => {
  if (id != null) { query.value = ""; searching.value = false; }
});
function label(x) {
  return [x.name, x.relationship_type_name, x.remark]
    .filter(Boolean)
    .join(" · ");
}
function time(x) {
  return x.created_at
    ? x.created_at.replace("T", " ").replaceAll("-", "/").slice(0, 19)
    : "";
}
const candidates = computed(() => {
  const words = (searching.value ? query.value : "").trim().split(/\s+/).filter(Boolean);
  return props.contacts
    .filter((x) => !props.excludeIds.includes(x.id))
    .filter(
      (x) =>
        !words.length ||
        words.some((w) => `${x.name} ${x.remark || ""}`.includes(w)),
    )
    .sort((a, b) => {
      if (props.selfFirst && a.is_self !== b.is_self) return a.is_self ? -1 : 1;
      return (
        (b.created_at || "").localeCompare(a.created_at || "") || b.id - a.id
      );
    });
});
let gesture = null;
function beginOption(event) {
  gesture = { pointerId: event.pointerId, x: event.clientX, y: event.clientY, scrollTop: menu.value?.scrollTop || 0, moved: false };
}
function moveOption(event) {
  if (!gesture || gesture.pointerId !== event.pointerId) return;
  if (Math.hypot(event.clientX - gesture.x, event.clientY - gesture.y) > 8) gesture.moved = true;
}
function choose(x, event) {
  const scrolled = gesture && Math.abs((menu.value?.scrollTop || 0) - gesture.scrollTop) > 2;
  // 原生触屏通常取消滚动后的 click，这里再保护浏览器合成点击。
  if (event?.detail !== 0 && (gesture?.moved || scrolled)) { gesture = null; return; }
  gesture = null;
  emit("update:modelValue", x.id);
  query.value = "";
  searching.value = false;
  open.value = false;
  inputElement.value?.blur();
}
function changedLayout() {
  nextTick(() => window.dispatchEvent(new Event("modal-family-layout")));
}
function onInput() {
  emit("update:modelValue", null);
  open.value = true;
  changedLayout();
}
function onFocus() {
  open.value = true;
  // 已有选择时可直接覆盖输入；保留未完成的搜索，不在重聚焦时清空。
  if (selected.value && !searching.value) inputElement.value?.select();
  changedLayout();
}
function toggleList() {
  const wasOpen = open.value;
  const wasTyping = document.activeElement === inputElement.value;
  inputElement.value?.blur();
  // 正在搜索时点箭头收起键盘，候选继续显示；其他时候正常开关列表。
  open.value = wasTyping ? true : !wasOpen;
  changedLayout();
}
function outside(event) {
  if (!root.value?.contains(event.target)) {
    open.value = false;
  }
}
function escape(event) {
  if (event.key === "Escape" && open.value) {
    event.stopImmediatePropagation();
    outside({ target: document.body });
  }
}
onMounted(() => {
  document.addEventListener("pointerdown", outside);
  document.addEventListener("pointermove", moveOption, { passive: true });
  document.addEventListener("keydown", escape, true);
});
onUnmounted(() => {
  document.removeEventListener("pointerdown", outside);
  document.removeEventListener("pointermove", moveOption);
  document.removeEventListener("keydown", escape, true);
});
</script>

<template>
  <div ref="root" class="contact-picker">
    <div class="picker-control">
      <input
        ref="inputElement"
        v-model="input"
        :placeholder="placeholder"
        autocomplete="off"
        role="combobox"
        aria-autocomplete="list"
        :aria-expanded="open"
        :aria-controls="menuId"
        @focus="onFocus"
        @input="onInput"
      />
      <button
        type="button"
        :aria-label="open ? '收起键盘或联系人列表' : '展开联系人'"
        :aria-expanded="open"
        :aria-controls="menuId"
        @pointerdown.prevent
        @click="toggleList"
      >
        ⌄
      </button>
    </div>
    <div v-if="open" :id="menuId" ref="menu" class="picker-menu" role="listbox" aria-label="联系人候选">
      <button
        v-for="x in candidates"
        :key="x.id"
        type="button"
        class="picker-option"
        role="option"
        :aria-selected="x.id === modelValue"
        @pointerdown="beginOption"
        @click="choose(x, $event)"
      >
        <span>{{ label(x) }}</span
        ><small>{{ time(x) }}</small>
      </button>
      <p v-if="!candidates.length" class="empty-small">没有符合条件的联系人</p>
    </div>
  </div>
</template>
