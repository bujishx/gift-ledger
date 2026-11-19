import assert from "node:assert/strict";
import {createHash} from "node:crypto";
import {readFileSync} from "node:fs";
import test from "node:test";
import {runInThisContext} from "node:vm";
import {makePacket, readIdentity, verifyReceipt, verifyTarget} from "../public/maintenance/age-transfer.js";

const vendor = readFileSync(new URL("../public/maintenance/vendor/age-0.3.1.min.js", import.meta.url));
const sha = (bytes) => createHash("sha256").update(bytes).digest("hex");
assert.equal(sha(vendor), "112306e7bc83bf28f47e45f632c5989c0ea979cdc7d0e5c71823b3eae7f086ae");
const age = runInThisContext("(function(){" + vendor.toString() + "\nreturn age;})()");
const canonical = (value) => JSON.stringify(value, Object.keys(value).sort()) + "\n";
const project = "site-test", instance_id = crypto.randomUUID(), job = crypto.randomUUID().replaceAll("-", "");

test("selected age files support comments and CRLF and refuse multiple, malformed and oversized identities", async () => {
  const line = await age.generateIdentity();
  const key = await readIdentity(new Blob(["# comment\r\n\r\n", line, "\r\n"]), age);
  assert.equal(key.secret, line + "\n");
  assert.equal(key.recipient, await age.identityToRecipient(line));
  assert.equal(key.hash, sha(line + "\n"));
  for (const input of [line + "\n" + line, "a".repeat(16385), line.slice(0, -1), "not a key"])
    await assert.rejects(readIdentity(new Blob([input]), age));
});

test("envelopes and receipts bind to the site, operation, selected identity and original request", async () => {
  const receiver = await age.generateIdentity(), original = await age.generateIdentity();
  const key = await readIdentity(new Blob([original]), age);
  const started = {format:"gift-ledger-gui-key-target-v1", project, instance_id,
    operation_id:crypto.randomUUID().replaceAll("-", ""), started_at:"2026-11-19T00:00:00+00:00"};
  const target = {...started, success:true, recipient:await age.identityToRecipient(receiver), started_sha256:sha(canonical(started))};
  const state = {target, instance_id};
  assert.equal(await verifyTarget(state, project), target);
  await assert.rejects(verifyTarget(state, project + "-other"));
  await assert.rejects(verifyTarget({...state, instance_id:crypto.randomUUID()}, project));
  await assert.rejects(verifyTarget({...state, target:{...target, started_at:"later"}}, project));

  const packet = await makePacket(age, target, job, key);
  assert.ok(!JSON.stringify(packet).includes(original));
  const cipher = Buffer.from(packet.ciphertext_b64, "base64");
  assert.equal(packet.request.ciphertext_sha256, sha(cipher));
  const decrypt = new age.Decrypter();
  decrypt.addIdentity(receiver);
  const payload = JSON.parse(await decrypt.decrypt(cipher, "text"));
  assert.equal(payload.identity, original + "\n");
  assert.equal(payload.operation_id, job);
  assert.equal(payload.target_recipient, target.recipient);

  const receipt = {format:"gift-ledger-gui-key-import-completed-v1", success:true, project,
    instance_id, operation_id:job, backup_recipient:key.recipient, identity_sha256:key.hash,
    request_sha256:sha(canonical(packet.request)), started_sha256:sha(canonical({operation_id:job,success:false}))};
  await verifyReceipt(receipt, target, key, packet.request);
  await verifyReceipt(receipt, target, key);
  for (const [field, value] of [["success",false], ["project","other"], ["instance_id",crypto.randomUUID()],
    ["operation_id","b".repeat(32)], ["identity_sha256","a".repeat(64)], ["request_sha256","a".repeat(64)]]) {
    await assert.rejects(verifyReceipt({...receipt, [field]:value}, target, key, packet.request));
  }
});
