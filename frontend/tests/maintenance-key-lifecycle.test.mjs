import assert from "node:assert/strict";
import {createHash, randomBytes} from "node:crypto";
import {readFileSync} from "node:fs";
import {runInThisContext} from "node:vm";
import test from "node:test";
import {existingPackage, openResponse, setRegisteredRecipient, verifyBackup} from "../public/maintenance/key-lifecycle.js";

const vendor = readFileSync(new URL("../public/maintenance/vendor/age-0.3.1.min.js", import.meta.url));
const age = runInThisContext("(function(){" + vendor + "\nreturn age;})()");
globalThis.location = new URL("https://192.0.2.10:9443/");
const digest = b => createHash("sha256").update(b).digest();
async function encrypt(recipient, bytes) {
  const e = new age.Encrypter();
  e.addRecipient(recipient);
  return e.encrypt(bytes);
}
test("administrator response binds to this browser, this site instance and the independently saved key", async () => {
  const secret = await age.generateIdentity(), identity = await age.generateIdentity();
  const recipient = await age.identityToRecipient(secret), owner = {instance:crypto.randomUUID(), project:"qa"};
  const job = {secret, recipient, action:"prepare"};
  const packet = {...owner, origin:location.origin, action:"prepare", version:1, operation:"9".repeat(32),
    receiver:recipient, identity:identity + "\n", recipient:await age.identityToRecipient(identity), challenge:"sealed"};
  const sealed = Buffer.from(await encrypt(recipient, JSON.stringify(packet))).toString("base64");
  assert.equal((await openResponse(age, sealed, job, owner)).recipient, packet.recipient);
  await assert.rejects(openResponse(age, sealed, job, {...owner, instance:crypto.randomUUID()}));
  await assert.rejects(openResponse(age, sealed, {...job, secret:identity}, owner));
  await assert.rejects(openResponse(age, sealed, {...job, action:"recover"}, owner));
});
test("saved key really decrypts the entire selected backup across browser chunk boundaries", async () => {
  const secret = await age.generateIdentity(), key = {secret}, operation = "4".repeat(32);
  const plain = randomBytes(2*1024*1024+37);
  const cipher = await encrypt(await age.identityToRecipient(secret), plain);
  const filename = "backup-" + operation + ".glgui.age";
  const file = new File([cipher], filename);
  let chain = Buffer.alloc(32);
  for (let i=0;i<plain.length;i+=1024*1024) chain = digest(Buffer.concat([chain, digest(plain.subarray(i, i+1024*1024))]));
  const proof = {filename, bytes:file.size, chain:chain.toString("hex")};
  await verifyBackup(age, file, key, proof);
  await assert.rejects(verifyBackup(age, file, {secret:await age.generateIdentity()}, proof));
  cipher[cipher.length-1] ^= 1;
  await assert.rejects(verifyBackup(age, new File([cipher], filename), key, proof));
  await assert.rejects(verifyBackup(age, file, key, {...proof, chain:"0".repeat(64)}));
});

test("existing-key package binds the old registration, receiver and site, encrypted only to the transfer key", async () => {
  const [browser, handover, current, saved] = await Promise.all(Array.from({length:4}, () => age.generateIdentity()));
  const [receiver, target, original, candidate] = await Promise.all([browser, handover, current, saved].map(key => age.identityToRecipient(key)));
  const owner = {project:"qa", instance:crypto.randomUUID()};
  const packet = {...owner, action:"existing", origin:location.origin, operation:"8".repeat(32), version:1,
    receiver, target, original};
  const wrapped = Buffer.from(await encrypt(receiver, JSON.stringify(packet))).toString("base64");
  const job = {action:"existing", secret:browser, recipient:receiver};
  setRegisteredRecipient(candidate);
  await assert.rejects(openResponse(age, wrapped, job, owner));
  setRegisteredRecipient(original);
  try {
    const offer = await openResponse(age, wrapped, job, owner);
    const {wire, token} = await existingPackage(age, offer, {secret:saved + "\n", recipient:candidate});
    const decryptor = new age.Decrypter();
    decryptor.addIdentity(handover);
    const decoded = JSON.parse(await decryptor.decrypt(Buffer.from(wire,"base64"), "text"));
    assert.deepEqual(decoded, {...packet, identity:saved + "\n", candidate, confirmation:token});
    assert.match(token, /^[0-9a-f]{48}$/);
    const oldKey = new age.Decrypter();
    oldKey.addIdentity(current);
    await assert.rejects(oldKey.decrypt(Buffer.from(wire,"base64")));
  } finally { setRegisteredRecipient(null); }
});
