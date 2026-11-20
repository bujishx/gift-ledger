import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {randomUUID} from "node:crypto";
import test from "node:test";
import {createContext, runInContext} from "node:vm";

const script = readFileSync(new URL("../../backend/app/resources/site_maintenance.js", import.meta.url), "utf8");

function element() {
  const children = new Map();
  return {
    textContent: "", value: "", hidden: false, disabled: false, dataset: {},
    addEventListener() {}, setAttribute() {}, querySelectorAll() { return []; },
    querySelector(selector) {
      if (!children.has(selector)) children.set(selector, element());
      return children.get(selector);
    },
    replaceChildren(child) { this.child = child; this.textContent = ""; },
  };
}

test("manual backup uses one operation per attempt and keeps pending tasks read-only", async () => {
  const document = element();
  document.documentElement = element();
  const find = selector => document.querySelector(selector);
  find("#backup-progress").content = {firstElementChild: {cloneNode: element}};
  const form = find("#maintenance");
  const start = form.querySelector('[value="backup"]'), check = form.querySelector('[value="status"]');
  const state = {project: "test-site", state: "idle", operation_id: null};
  const posts = [];
  let offline = false;
  const ctx = createContext({
    document, location: {origin: "https://example.test"}, crypto: {randomUUID}, AbortController,
    window: {matchMedia: element, addEventListener() {}}, createOwnerConsole: () => ({setOwner() {}}),
    async fetch(url, options) {
      if (url.endsWith("/access-methods")) return new Response(null, {status: 404});
      if (offline) throw Error("offline");
      if (url.endsWith("/backup")) {
        const request = JSON.parse(options.body);
        posts.push(request);
        Object.assign(state, {state: "queued", operation_id: request.operation_id});
      }
      return Response.json(state);
    },
  });
  runInContext(script, ctx);
  await new Promise(resolve => setImmediate(resolve)); // Resolve the legacy access-methods lookup.
  find("#token").value = "test-credential";
  const submit = () => runInContext("readOrStartBackup(true)", ctx);
  const status = () => runInContext("readOrStartBackup(false)", ctx);
  const progress = selector => find("#result").child.querySelector(selector).textContent;

  assert.equal(start.disabled, false);
  await Promise.all([submit(), submit()]);
  assert.equal(posts.length, 1);
  assert.equal(start.disabled, true);
  assert.equal(check.disabled, false);
  assert.equal(check.className, "primary");
  assert.equal(start.textContent, "等待执行");
  assert.equal(progress("strong"), "备份已提交，无需重复点击。");
  assert.equal(progress("[data-project]"), state.project);
  assert.equal(progress("[data-operation]"), posts[0].operation_id);

  state.state = "running";
  await status();
  assert.equal(start.textContent, "备份中");
  assert.equal(progress("strong"), "正在备份，请稍候。");
  offline = true;
  await status();
  assert.equal(start.disabled, true);
  assert.equal(check.disabled, false);
  assert.match(find("#result").textContent, /暂时无法联系本站/);
  await submit();
  assert.equal(posts.length, 1);

  offline = false;
  state.state = "needs_review";
  await status();
  assert.equal(start.disabled, true);
  await submit();
  assert.equal(posts.length, 1);

  state.state = "succeeded";
  await status();
  assert.equal(start.disabled, false);
  assert.equal(start.textContent, "开始备份");
  assert.equal(check.className, "outline");
  await submit();
  assert.equal(posts.length, 2);
  assert.equal(posts[1].after_operation_id, posts[0].operation_id);
  assert.notEqual(posts[1].operation_id, posts[0].operation_id);
});
