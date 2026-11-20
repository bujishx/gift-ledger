// Server administrator authorizes only a public one-use browser recipient.
// Keys are saved in the browser; the server's terminal displays sealed data only.
import {KeyError, loadAge, readIdentity} from "./age-transfer.js";

const raw = value => Uint8Array.from(atob(value), ch => ch.charCodeAt(0));
const hex = bytes => Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("");
const hash = async bytes => new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
const check = (ok, message) => { if (!ok) throw new KeyError(message); };
const blank = () => ({target:null, action:null, secret:null, recipient:null, packet:null, downloaded:false});
const pub = /^age1[023456789acdefghjklmnpqrstuvwxyz]{58}$/;
let owner = null, job = blank(), epoch = 0, registered = null;
export function setRegisteredRecipient(value, state) {
  const next = pub.test(value) ? value : null;
  if (typeof document !== "undefined") {
    if (registered && next && registered !== next) reset();
    el("actions").hidden = !next;
    el("binding-note").hidden = !!next;
    el("binding-note").textContent = state === "unregistered" ? "登记本站备份钥匙后，可在这里补领或更换。" :
      state ? "暂时无法核实本站登记状态。" : "正在读取本站钥匙登记状态。";
    el("clear").hidden = !next && !job.action && !el("note").textContent;
  }
  registered = next;
}

const utf8 = new TextEncoder();
function confirmationCode(token, name) {
  if (!name || /AGE-SECRET-KEY-/i.test(name) || name === "." || name === ".." || /[/\\\p{C}\p{Z}]/u.test(name.replaceAll(" ", "")) || utf8.encode(name).length > 255) return token;
  return token + "." + btoa(String.fromCharCode(...utf8.encode(name)));
}

export async function existingPackage(age, target, selected) {
  const token = hex(crypto.getRandomValues(new Uint8Array(24)));
  const plain = utf8.encode(JSON.stringify({...target, candidate:selected.recipient, identity:selected.secret, confirmation:token}));
  try {
    const writer = new age.Encrypter();
    writer.addRecipient(target.target);
    return {wire:btoa(String.fromCharCode(...await writer.encrypt(plain))), token};
  } finally {
    plain.fill(0);
  }
}

export async function openResponse(age, sealed, job, owner) {
  check(sealed.length < 16384, "加密回执过长。");
  const box = new age.Decrypter();
  box.addIdentity(job.secret);
  const data = JSON.parse(await box.decrypt(raw(sealed), "text"));
  check(data.version === 1 && data.action === job.action && data.project === owner.project &&
    data.instance === owner.instance && data.origin === location.origin && data.receiver === job.recipient &&
    /^[0-9a-f]{32}$/.test(data.operation), "回执属于另一站、另一操作或另一接收码。");
  if (job.action === "existing") {
    check(pub.test(data.target) && pub.test(data.original) && data.target !== data.original &&
      data.target !== job.recipient && (!registered || registered === data.original),
      "本站当前的登记或交接公钥发生变化。请重新打开维护页核对。");
    return data;
  }
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
  for (const id of ["receive", "sealed", "key", "backup", "existing-file", "package"]) el(id).value = "";
  for (const id of ["exchange", "verify", "choose-backup", "sources", "existing-field", "package-field"]) el(id).hidden = true;
  el("note").textContent = "";
  job = blank();
}

export function setOwner(session) {
  reset();
  owner = session;
  setRegisteredRecipient(null);
  el("panel").hidden = !session?.instance;
}

function choices() {
  reset();
  el("sources").hidden = false;
  el("note").textContent = "请选择今后备份要用的钥匙。实际解密核验首份备份后才会切换。";
}

function chooseFile() {
  choices();
  el("existing-field").hidden = false;
  el("note").textContent = "选择你自己保存的备份钥匙，可来自这个站或其他站。";
}

async function begin(action) {
  const file = action === "existing" ? el("existing-file").files[0] : null;
  const session = owner, version = epoch;
  const age = await loadAge();
  const selected = action === "existing" ? await readIdentity(file, age) : null;
  if (version !== epoch || owner !== session) return;
  if (selected && selected.recipient === registered) {
    reset();
    el("note").textContent = "这就是本站当前的钥匙，无需更换。";
    return;
  }
  reset();
  const started = epoch;
  const secret = await age.generateIdentity();
  const publicKey = await age.identityToRecipient(secret);
  if (started !== epoch) return;
  job = {...blank(), action, secret, recipient:publicKey, file, selectedRecipient:selected?.recipient};
  el("exchange").hidden = false;
  el("receive").value = publicKey;
  const commands = {recover:"recover-key", prepare:"prepare-key-change", existing:"prepare-existing-key-change"};
  el("command").textContent = "sudo /usr/bin/python3 -I /root/.giftledger-maintenance/" +
    owner.project + "/maintenance-install.pyz " + commands[action] + " --project " + owner.project;
  el("note").textContent = "保持本页打开。在本站服务器的管理员终端运行所示命令，核对项目和网址，输入接收码。" + (action === "recover" ? "若站内没有与登记钥匙匹配的已有备份，请先做一次常规备份。" : "");
}

function finishCommand(project, code) {
  return "换钥确认码 " + code + "\n在服务器管理员终端执行以下命令，输入确认码。网站将为首份备份短暂停用，实际解密核验通过后才切换。\nsudo /usr/bin/python3 -I /root/.giftledger-maintenance/" +
    project + "/maintenance-install.pyz finish-key-change --project " + project;
}

async function open() {
  const active = job, session = owner;
  check(active.secret, "请先生成本次接收码。");
  const age = await loadAge();
  const data = await openResponse(age, el("sealed").value.trim(), active, session);
  if (active !== job) return;
  if (active.action === "existing") {
    const saved = await readIdentity(active.file, age);
    check(saved.recipient === active.selectedRecipient, "所选文件发生变化，请重新选择。");
    if (saved.recipient === data.original) {
      reset();
      el("note").textContent = "这就是本站当前的钥匙，无需更换。可在服务器终端按回车退出。";
      return;
    }
    const {wire, token} = await existingPackage(age, data, saved);
    if (active !== job) return;
    el("package-field").hidden = false;
    el("package").value = wire;
    el("note").textContent = "先把加密钥匙包复制到仍在等待的管理员终端。看到待启用公钥为 " + saved.recipient +
      " 后，再使用下方命令。\n" + finishCommand(session.project, confirmationCode(token, active.file.name));
    // One package per opened response: reopening must not invalidate the displayed confirmation.
    el("exchange").hidden = true;
    active.file = null;
    return;
  }
  const filename = active.action === "prepare"
    ? "key-" + hex(await hash(utf8.encode(data.identity))).slice(0, 32) + ".agekey"
    : session.project + "-" + data.instance.slice(0, 8) + "-" + data.recipient.slice(-8) + ".agekey";
  if (active !== job) return;
  const link = el("download");
  if (link.hasAttribute("href")) URL.revokeObjectURL(link.href);
  link.href = URL.createObjectURL(new Blob([data.identity], {type:"application/octet-stream"}));
  link.download = filename;
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
    const code = confirmationCode(token, el("key").files[0].name);
    const project = owner.project;
    reset();
    el("note").textContent = finishCommand(project, code);
  }
}

export function bind() {
  const panel = el("panel");
  for (const [id, fn] of [["recover", () => begin("recover")], ["prepare", choices],
                           ["generate", () => begin("prepare")], ["existing", chooseFile],
                           ["authorize-existing", () => begin("existing")],
                           ["open", open], ["confirm", confirm], ["clear", reset]]) {
    el(id).addEventListener("click", async () => {
      if (!owner) return;
      const buttons = Array.from(panel.querySelectorAll("button"));
      buttons.forEach(b => { b.disabled = true; });
      const session = owner;
      try { await fn(); }
      catch (err) {
        if (session === owner) el("note").textContent = err instanceof KeyError ? err.message : "未完成。请核对接收码、加密回执、钥匙和备份后再试。";
      } finally { buttons.forEach(b => { b.disabled = false; }); }
    });
  }
  el("download").addEventListener("click", () => { job.downloaded = true; });
}
