import assert from "node:assert/strict";
import test from "node:test";

import {
  GIFT_SEARCH_SCOPE,
  clampCardPosition,
  eventContextParams,
  giftClass,
  giftMark,
  isReauthError,
  ledgerContextParams,
  pickCardPosition,
  queryContactLabel,
} from "../src/lib/ledgerView.js";
import { queryString } from "../src/lib/query.js";

test("F01 搜索范围文案包含完整范围与空格说明", () => {
  for (const part of [
    "事主姓名",
    "事主姓名或备注",
    "事件备注",
    "随礼人姓名或备注",
    "空格分隔关键词",
  ]) {
    assert.ok(GIFT_SEARCH_SCOPE.includes(part), `缺少：${part}`);
  }
});

test("F03 礼金方向单字与颜色：支出红、收入绿", () => {
  assert.equal(giftMark("expense"), "支");
  assert.equal(giftMark("income"), "收");
  assert.equal(giftClass("expense"), "expense");
  assert.equal(giftClass("income"), "income");
});

test("F03 随礼人用 姓名(关系类型·联系人备注) 区分同名人", () => {
  assert.equal(
    queryContactLabel({
      name: "李建国",
      remark: "老家东院",
      relationship_type: { id: 3, name: "邻居" },
    }),
    "李建国(邻居·老家东院)",
  );
  assert.equal(
    queryContactLabel({
      name: "王敏",
      remark: null,
      relationship_type: { id: 4, name: "同学" },
    }),
    "王敏(同学)",
  );
  assert.equal(queryContactLabel(null), "—");
});

test("密码再验证只在服务端明确要求时触发", () => {
  assert.equal(isReauthError({ code: "GIFT_CHANGE_REAUTH_REQUIRED" }), true);
  assert.equal(isReauthError({ status: 403, code: "SELF_CONTACT_REQUIRED" }), false);
  assert.equal(isReauthError({ status: 401 }), false);
  assert.equal(isReauthError(undefined), false);
});

test("查账临时精确筛选互斥：事件 ID 或联系人 ID", () => {
  assert.deepEqual(ledgerContextParams(null), { event_id: "", contact_id: "" });
  assert.deepEqual(ledgerContextParams({ kind: "event", id: 101 }), {
    event_id: 101,
    contact_id: "",
  });
  assert.deepEqual(ledgerContextParams({ kind: "contact", id: 10 }), {
    event_id: "",
    contact_id: 10,
  });
});

test("查事临时精确筛选传递事主联系人 ID", () => {
  assert.deepEqual(eventContextParams(null), { host_contact_id: "" });
  assert.deepEqual(eventContextParams({ id: 10 }), { host_contact_id: 10 });
});

test("临时条件只按 ID 传参，不把姓名当关键词", () => {
  const params = {
    keyword: "",
    ...ledgerContextParams({ kind: "contact", id: 11 }),
  };
  assert.equal(queryString(params), "?contact_id=11");
  assert.ok(!queryString(params).includes("%E6%9D%8E"));
});

test("清除临时条件后查询串不再带精确 ID", () => {
  assert.equal(queryString({ ...ledgerContextParams(null), limit: 20 }), "?limit=20");
});

test("悬浮卡定位：优先贴在锚点右侧", () => {
  const position = pickCardPosition(
    { left: 100, top: 100, right: 200, bottom: 130 },
    { width: 300, height: 200 },
    { width: 1200, height: 800 },
  );
  assert.deepEqual(position, { left: 209, top: 100 });
});

test("悬浮卡定位：右侧放不下时换到左侧", () => {
  const position = pickCardPosition(
    { left: 900, top: 100, right: 1000, bottom: 130 },
    { width: 300, height: 200 },
    { width: 1100, height: 800 },
  );
  assert.deepEqual(position, { left: 591, top: 100 });
});

test("悬浮卡定位：底部空间不足时与锚点底部对齐", () => {
  const position = pickCardPosition(
    { left: 100, top: 700, right: 200, bottom: 730 },
    { width: 300, height: 200 },
    { width: 1200, height: 800 },
  );
  assert.deepEqual(position, { left: 209, top: 530 });
});

test("悬浮卡定位：始终约束在视口内", () => {
  assert.deepEqual(
    clampCardPosition(-40, -90, { width: 300, height: 200 }, { width: 1200, height: 800 }),
    { left: 8, top: 8 },
  );
  assert.deepEqual(
    clampCardPosition(5000, 5000, { width: 300, height: 200 }, { width: 1200, height: 800 }),
    { left: 892, top: 592 },
  );
});

test("无锚点时卡片居中", () => {
  const position = pickCardPosition(
    null,
    { width: 300, height: 200 },
    { width: 1200, height: 800 },
  );
  assert.deepEqual(position, { left: 450, top: 300 });
});
