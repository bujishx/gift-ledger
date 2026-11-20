// Server administrator authorizes only a public one-use browser recipient.
// Keys are saved in the browser; the server's terminal displays sealed data only.
import {loadAge, readIdentity} from "./age-transfer.js";

const raw = value => Uint8Array.from(atob(value), ch => ch.charCodeAt(0));
const hex = bytes => Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("");
const hash = async bytes => new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
const check = (ok, message) => { if (!ok) throw new Error(message); };
const blank = () => ({target:null, action:null, secret:null, recipient:null, packet:null, downloaded:false});
let owner = null, job = blank(), epoch = 0;

export async function openResponse(age, sealed, job, owner) {
  check(sealed.length < 16384, "加密回执过长。");
  const box = new age.Decrypter();
  box.addIdentity(job.secret);
  const data = JSON.parse(await box.decrypt(raw(sealed), "text"));
  check(data.version === 1 && data.action === job.action && data.project === owner.project &&
    data.instance === owner.instance && data.origin === location.origin && data.receiver === job.recipient &&
    /^[0-9a-f]{32}$/.test(data.operation), "回执属于另一站、另一操作或另一接收码。");
  const key = await readIdentity(new Blob([data.identity]), age);
  check(key.recipient === data.recipient, "回执中的钥匙与登记公钥不符。");
  if (job.action === "recover") {
    const v = data.verified;
    check(v && /^[0-9a-f]{32}$/.test(v.backup) && v.filename === "backup-" + v.backup + ".glgui.age" &&
      Number.isSafeInteger(v.bytes) && v.bytes > 0 && /^[0-9a-f]{64}$/.test(v.chain), "已有备份核验信息不完整。");
  } else check(typeof data.challenge === "string", "换钥确认资料不完整。");
  return data;
}

export async function verifyBackup(age, file, key, proof) {
  check(file && file.name === proof.filename && file.size === proof.bytes, "所选密文与指定备份不符。请从该备份 ZIP 解压后选择 .age 文件。");
  const opener = new age.Decrypter();
  opener.addIdentity(key.secret.trim());
  const plain = (await opener.decrypt(file.stream())).getReader();
  // Exactly the same 1 MiB blocks as the server, independent of browser stream chunking.
  // Authenticated age output is consumed through the final authenticated byte; plaintext stays in memory.
  const block = new Uint8Array(1024 * 1024);
  const pair = new Uint8Array(64);
  let used = 0;
  async function finishBlock() {
    pair.set(await hash(block.subarray(0, used)), 32);
    pair.set(await hash(pair), 0);
    used = 0;
  }
  try {
    for (;;) {
      const {done, value} = await plain.read();
      if (done) break;
      for (let at = 0; at < value.length;) {
        const take = Math.min(block.length - used, value.length - at);
        block.set(value.subarray(at, at + take), used);
        used += take; at += take;
        if (used === block.length) await finishBlock();
      }
    }
    if (used) await finishBlock();
    check(hex(pair.subarray(0, 32)) === proof.chain, "实际解密内容与服务器已核验备份不符。");
  } finally {
    block.fill(0);
    await plain.cancel().catch(() => {});
    plain.releaseLock();
  }
}

const el = id => document.getElementById("lifecycle-" + id);
function reset() {
  epoch++;
  const link = el("download");
  if (link.hasAttribute("href")) URL.revokeObjectURL(link.href);
  link.removeAttribute("href");
  link.hidden = true;
  for (const id of ["receive", "sealed", "key", "backup"]) el(id).value = "";
  for (const id of ["exchange", "verify", "choose-backup"]) el(id).hidden = true;
  el("note").textContent = "";
  job = blank();
}

export function setOwner(session) {
  reset();
  owner = session;
  el("panel").hidden = !session?.instance;
}

async function begin(action) {
  reset();
  const version = epoch;
  const age = await loadAge();
  const secret = await age.generateIdentity();
  const publicKey = await age.identityToRecipient(secret);
  if (version !== epoch) return;
  job = {...blank(), action, secret, recipient:publicKey};
  el("exchange").hidden = false;
  el("receive").value = publicKey;
  const command = action === "recover" ? "recover-key" : "prepare-key-change";
  el("command").textContent = "sudo /usr/bin/python3 -I /root/.giftledger-maintenance/" +
    owner.project + "/maintenance-install.pyz " + command + " --project " + owner.project;
  el("note").textContent = "保持本页打开。在本站服务器的管理员终端运行所示命令，核对项目和网址，输入接收码。" + (action === "recover" ? "若站内没有与登记钥匙匹配的已有备份，请先做一次常规备份。" : "");
}

async function open() {
  const active = job, session = owner;
  check(active.secret, "请先生成本次接收码。");
  const age = await loadAge();
  const data = await openResponse(age, el("sealed").value.trim(), active, session);
  if (active !== job) return;
  const link = el("download");
  if (link.hasAttribute("href")) URL.revokeObjectURL(link.href);
  link.href = URL.createObjectURL(new Blob([data.identity], {type:"application/octet-stream"}));
  link.download = session.project + "-" + data.instance.slice(0, 8) + "-" + data.recipient.slice(-8) + ".agekey";
  link.hidden = false;
  delete data.identity;
  active.packet = data;
  active.downloaded = false;
  el("verify").hidden = false;
  el("choose-backup").hidden = active.action !== "recover";
  el("note").textContent = active.action === "recover"
    ? "请保存补领的钥匙，再打开保存好的文件。解压 " + data.verified.backup + " 的备份，选择其中 .age 密文进行实际解密核验。"
    : "请先保存新钥匙，再从保存位置重新选择同一文件以取得换钥确认码。";
}

async function confirm() {
  const active = job;
  check(active.downloaded && active.packet, "请先下载并保存本次钥匙。");
  const age = await loadAge();
  const saved = await readIdentity(el("key").files[0], age);
  check(saved.recipient === active.packet.recipient, "请选择本次实际保存的钥匙文件。");
  if (active.action === "recover") {
    el("note").textContent = "正在本机实际解密并核对整份备份…";
    await verifyBackup(age, el("backup").files[0], saved, active.packet.verified);
    if (active !== job) return;
    reset();
    el("note").textContent = "补领成功。保存的钥匙已实际解开指定备份，内容与本站回执一致。";
  } else {
    const opener = new age.Decrypter();
    opener.addIdentity(saved.secret.trim());
    const token = await opener.decrypt(raw(active.packet.challenge), "text");
    check(/^[0-9a-f]{48}$/.test(token), "重新打开保存文件后仍未能核对确认码。");
    if (active !== job) return;
    const project = owner.project;
    reset();
    el("note").textContent = "保存确认码 " + token + "\n在服务器管理员终端运行以下命令，输入确认码。网站会为首份新备份短暂停用，实际解密核验通过后启用新钥匙。\nsudo /usr/bin/python3 -I /root/.giftledger-maintenance/" +
      project + "/maintenance-install.pyz finish-key-change --project " + project;
  }
}

export function bind() {
  const panel = el("panel");
  for (const [id, fn] of [["recover", () => begin("recover")], ["prepare", () => begin("prepare")],
                           ["open", open], ["confirm", confirm], ["clear", reset]]) {
    el(id).addEventListener("click", async () => {
      if (!owner) return;
      const buttons = Array.from(panel.querySelectorAll("button"));
      buttons.forEach(b => { b.disabled = true; });
      const session = owner;
      try { await fn(); }
      catch (err) {
        if (session === owner) el("note").textContent = "未完成。请核对接收码、加密回执、钥匙和备份后再试。";
      } finally { buttons.forEach(b => { b.disabled = false; }); }
    });
  }
  el("download").addEventListener("click", () => { job.downloaded = true; });
}
