import assert from "node:assert/strict";
import test from "node:test";
import { nextTick } from "vue";
import { useFloatingCard } from "../src/composables/useFloatingCard.js";

function rect(left = 20, top = 300, width = 370, height = 82) {
  return { left, top, width, height, right: left + width, bottom: top + height };
}

function setup(t, { width = 390, height = 844, navHeight = 63, rowHeight = 82, cardHeight = 470 } = {}) {
  const oldWindow = globalThis.window, oldDocument = globalThis.document;
  const state = { navHeight, selected: null, focused: [], inert: false };
  const view = { innerWidth: width, innerHeight: height };
  const previous = {
    isConnected: true, disabled: false,
    closest() { return state.inert ? {} : null; },
    focus() { state.focused.push("row"); },
  };
  const document = {
    activeElement: previous,
    querySelector(selector) {
      return selector === ".bottom-nav" ? { getBoundingClientRect: () => rect(0, view.innerHeight - state.navHeight, view.innerWidth, state.navHeight) } : null;
    },
    querySelectorAll() { return state.selected ? [state.selected] : []; },
  };
  globalThis.window = view;
  globalThis.document = document;
  t.after(() => {
    if (oldWindow === undefined) delete globalThis.window; else globalThis.window = oldWindow;
    if (oldDocument === undefined) delete globalThis.document; else globalThis.document = oldDocument;
  });
  const floating = useFloatingCard();
  const row = { isConnected: true, rect: rect(100, 100, 200, rowHeight), getBoundingClientRect() { return this.rect; } };
  state.selected = row;
  const card = {
    isConnected: true,
    get offsetWidth() { return view.innerWidth <= 700 ? view.innerWidth - 16 : view.innerWidth <= 1100 ? 460 : 400; },
    get offsetHeight() { return Math.min(cardHeight, view.innerHeight - floating.position.navHeight - floating.position.cardGap - 16); },
    getBoundingClientRect() { return rect(0, 0, this.offsetWidth, this.offsetHeight); },
    focus() { state.focused.push("card"); },
  };
  floating.setRef(card);
  return { floating, row, card, view, previous, state };
}

function dragEvent(id = 1, target = {}) {
  const capture = { id: null, prevented: 0 };
  const handle = {
    setPointerCapture(id) { capture.id = id; },
    hasPointerCapture(id) { return capture.id === id; },
    releasePointerCapture() { capture.id = null; },
  };
  return {
    capture,
    event: { pointerId: id, button: 0, clientX: 100, clientY: 100, currentTarget: handle, target: { closest: () => null, ...target }, preventDefault() { capture.prevented++; } },
  };
}

test("手机间距采用目标行高的一半，导航测量含安全区且短屏不越过导航", async (t) => {
  const { floating, row, card, view, state } = setup(t);
  await floating.place(floating.anchorFrom({ currentTarget: row }));
  assert.equal(floating.position.navHeight, 63);
  assert.equal(floating.position.cardGap, 41);
  assert.equal(view.innerHeight - floating.position.top - card.offsetHeight, 104);
  row.rect = rect(8, 220, 374, 96);
  state.navHeight = 79;
  view.innerHeight = 600;
  floating.keepInView();
  await nextTick();
  assert.equal(floating.position.navHeight, 79);
  assert.equal(floating.position.cardGap, 48);
  assert.ok(floating.position.top >= 8);
  assert.equal(floating.position.top + card.offsetHeight, 600 - 79 - 48);
  assert.equal(state.focused.filter(x => x === "card").length, 1, "resize不能抢走已有按钮焦点");
});

test("平板701至1100居中不拖动，1101起保留电脑锚点定位", async (t) => {
  const { floating, row, card, view } = setup(t, { width: 1100, height: 1000 });
  await floating.place(floating.anchorFrom({ currentTarget: row }));
  assert.equal(floating.position.left, (1100 - card.offsetWidth) / 2);
  assert.equal(floating.position.top, (1000 - card.offsetHeight) / 2);
  const tabletDrag = dragEvent();
  floating.startDrag(tabletDrag.event);
  assert.equal(tabletDrag.capture.prevented, 0);
  view.innerWidth = 701;
  floating.keepInView();
  await nextTick();
  assert.equal(floating.position.left, (701 - card.offsetWidth) / 2);
  view.innerWidth = 1101;
  floating.keepInView();
  await nextTick();
  assert.equal(floating.position.left, row.rect.right + 9);
  assert.equal(floating.position.top, row.rect.top);
  assert.equal(floating.position.navHeight, 0);
  assert.equal(floating.position.cardGap, 0);
});

test("手机程序化打开使用目标行DOMRect，不继承来源事件行高度", async (t) => {
  const { floating, row } = setup(t);
  const source = { isConnected: true, getBoundingClientRect: () => rect(8, 120, 374, 136) };
  floating.anchorFrom({ currentTarget: source });
  await floating.place(row.getBoundingClientRect());
  assert.equal(floating.position.cardGap, 41);
  row.rect = rect(8, 100, 374, 90);
  floating.keepInView();
  await nextTick();
  assert.equal(floating.position.cardGap, 45);
});

test("电脑拖动限制在视口内，跨入平板释放指针捕获", async (t) => {
  const { floating, row, card, view } = setup(t, { width: 1440, height: 900 });
  await floating.place(floating.anchorFrom({ currentTarget: row }));
  const { event, capture } = dragEvent(12);
  floating.startDrag(event);
  assert.equal(capture.id, 12);
  floating.moveDrag({ pointerId: 12, clientX: 9000, clientY: 9000 });
  assert.equal(floating.position.left, 1440 - card.offsetWidth - 8);
  assert.equal(floating.position.top, 900 - card.offsetHeight - 8);
  view.innerWidth = 820;
  floating.keepInView();
  await nextTick();
  assert.equal(capture.id, null);
  assert.equal(floating.position.left, (820 - card.offsetWidth) / 2);
  assert.equal(floating.position.top, (900 - card.offsetHeight) / 2);
});

test("电脑卡头的按钮不启动拖动", async (t) => {
  const { floating, row } = setup(t, { width: 1440 });
  await floating.place(row.getBoundingClientRect());
  const { event, capture } = dragEvent(3, { closest: () => ({}) });
  floating.startDrag(event);
  assert.equal(capture.prevented, 0);
  assert.equal(capture.id, null);
});

test("关闭等inert移除后才归还焦点，已卸载卡不影响归还", async (t) => {
  const { floating, row, state } = setup(t);
  await floating.place(row.getBoundingClientRect());
  state.inert = true;
  floating.restoreFocus();
  assert.deepEqual(state.focused, ["card"]);
  floating.setRef(null);
  state.inert = false;
  await nextTick();
  assert.deepEqual(state.focused, ["card", "row"]);
  assert.equal(floating.position.navHeight, 0);
  assert.equal(floating.position.cardGap, 0);
});

test("原触发项被禁用或仍inert时不强行归还焦点", async (t) => {
  const { floating, row, previous, state } = setup(t);
  previous.disabled = true;
  await floating.place(row.getBoundingClientRect());
  floating.restoreFocus();
  await nextTick();
  assert.deepEqual(state.focused, ["card"]);
  previous.disabled = false;
  globalThis.document.activeElement = previous;
  await floating.place(row.getBoundingClientRect());
  state.inert = true;
  floating.restoreFocus();
  await nextTick();
  assert.deepEqual(state.focused, ["card", "card"]);
});

test("关闭可取消未完成定位，多个卡片的间距互不污染", async (t) => {
  const { floating, row, card, state } = setup(t);
  const pending = floating.place(row.getBoundingClientRect());
  floating.setRef(null);
  await pending;
  assert.deepEqual(state.focused, []);
  const other = useFloatingCard();
  other.setRef({ card });
  await other.place(rect(8, 100, 374, 82));
  assert.equal(other.position.cardGap, 41);
  assert.equal(floating.position.cardGap, 0);
  other.setRef(null);
  assert.equal(other.position.navHeight, 0);
});
