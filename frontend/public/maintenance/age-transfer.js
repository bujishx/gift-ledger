// Wire formats and canonical JSON match deploy/single_yaml/mac_backup_key.py.
const utf8 = new TextEncoder();
const recipientPattern = /^age1[023456789acdefghjklmnpqrstuvwxyz]{58}$/;
const jobPattern = /^[0-9a-f]{32}$/;
const shaPattern = /^[0-9a-f]{64}$/;
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
function ownFields(object, fields) {
  const names = fields.split(" ");
  return object && typeof object === "object" && Object.keys(object).length === names.length &&
    names.every(name => Object.hasOwn(object, name));
}

export class KeyError extends Error {}
function check(test, message) { if (!test) throw new KeyError(message); }
const canonical = (value) => utf8.encode(JSON.stringify(value, Object.keys(value).sort()) + "\n");
async function digest(bytes) {
  const sum = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(sum), b => b.toString(16).padStart(2, "0")).join("");
}

let ageLoading;
export function loadAge() {
  if (!ageLoading) {
    ageLoading = new Promise((resolve, reject) => {
      const tag = document.createElement("script");
      // Unmodified https://github.com/FiloSottile/typage/releases/tag/v0.3.1 asset, with its licenses in vendor/.
      tag.src = "/maintenance/vendor/age-0.3.1.min.js";
      tag.integrity = "sha384-rRDwbrBZetZZjbqCGJ4kXCQXqT1rkVQoNEFI6TmqFBB5O6xpUWXwSMphvW2jb4/J";
      tag.onload = () => resolve(window.age);
      tag.onerror = () => {
        ageLoading = undefined;
        tag.remove();
        reject(new KeyError("无法载入本站的钥匙工具，请重新打开维护页再试。"));
      };
      document.head.append(tag);
    });
  }
  return ageLoading;
}

export async function readIdentity(file, age) {
  check(file && file.size > 0 && file.size <= 16384, "请选择不超过 16 KB、只含一把原钥匙的文件。");
  const bytes = new Uint8Array(await file.arrayBuffer());
  const invalid = "请选择只含一把经典 age 原钥匙的文件，例如 identity.txt 或 .agekey。";
  try {
    check(bytes.every(b => b < 128), invalid);
    const lines = new TextDecoder().decode(bytes).split(/\r\n|\r|\n/)
      .filter(line => line.trim() && !line.startsWith("#")).map(line => line.trim());
    check(lines.length === 1 && /^AGE-SECRET-KEY-1[A-Z0-9]{40,100}$/.test(lines[0]), invalid);
    let recipient;
    try { recipient = await age.identityToRecipient(lines[0]); } catch { throw new KeyError(invalid); }
    check(recipientPattern.test(recipient), invalid);
    const secret = lines[0] + "\n";
    return {secret, recipient, hash:await digest(utf8.encode(secret))};
  } finally {
    bytes.fill(0);
  }
}

export async function verifyTarget(record, project) {
  const t = record.target;
  const error = "站点身份或交接公钥核对失败，登记已停止。";
  check(ownFields(t, "format project instance_id operation_id started_at recipient started_sha256 success") &&
    t.format === "gift-ledger-gui-key-target-v1" && t.success === true &&
    t.project === project && t.instance_id === record.instance_id && uuidPattern.test(t.instance_id) &&
    jobPattern.test(t.operation_id) && recipientPattern.test(t.recipient) && typeof t.started_at === "string", error);
  const {format, instance_id, operation_id, started_at} = t;
  check(t.started_sha256 === await digest(canonical({format, project, instance_id, operation_id, started_at})), error);
  return t;
}

export async function makePacket(age, target, job, key) {
  const fields = {project:target.project, instance_id:target.instance_id, operation_id:job,
    target_recipient:target.recipient, backup_recipient:key.recipient};
  const plain = canonical({...fields, format:"gift-ledger-gui-key-transfer-payload-v1", identity:key.secret});
  const encrypter = new age.Encrypter();
  encrypter.addRecipient(target.recipient);
  let cipher;
  try { cipher = await encrypter.encrypt(plain); } finally { plain.fill(0); }
  const request = {...fields, format:"gift-ledger-gui-key-import-request-v1",
    ciphertext:"key-transfer-" + job + ".age", ciphertext_sha256:await digest(cipher)};
  return {request, ciphertext_b64:btoa(String.fromCharCode(...cipher))};
}

export async function verifyReceipt(receipt, target, key, request) {
  const invalid = "本站登记回执核对失败，请按本站维护说明查看登记状态。";
  check(ownFields(receipt, "format success project instance_id operation_id backup_recipient request_sha256 started_sha256 identity_sha256") &&
    receipt.format === "gift-ledger-gui-key-import-completed-v1" && receipt.success === true &&
    receipt.project === target.project && receipt.instance_id === target.instance_id &&
    jobPattern.test(receipt.operation_id) && shaPattern.test(receipt.request_sha256), invalid);
  check(receipt.started_sha256 === await digest(canonical({operation_id:receipt.operation_id,success:false})), invalid);
  if (request) {
    check(receipt.operation_id === request.operation_id && receipt.request_sha256 === await digest(canonical(request)), invalid);
  }
  check(receipt.backup_recipient === key.recipient && receipt.identity_sha256 === key.hash,
    request ? invalid : "本站已登记另一把原钥匙。请重新选择与本站登记记录相符的原钥匙。");
}
