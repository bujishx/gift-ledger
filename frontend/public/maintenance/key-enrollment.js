import * as lifecycle from "./key-lifecycle.js";
lifecycle.bind();
import {KeyError, loadAge, makePacket, readIdentity, verifyReceipt, verifyTarget} from "./age-transfer.js";

const panel = document.querySelector("#key-enrollment");
const element = id => document.getElementById("key-" + id);
const file = element("file");
const download = element("download");
const feedback = element("feedback");
const retry = element("retry");
let owner, generatedRecipient, operation, generation = 0, busy = false, downloadClicked = false;
let binding = null, sampleTimer, requestNumber = 0;
let justEnrolled = null;
const checkFile = element("check-file");

function verifiedText(proof) {
  if (proof?.state === "none") return "尚无解密核验通过的记录";
  if (proof?.state !== "verified") return "暂时无法核实";
  const date = proof.at && new Date(proof.at);
  return date && Number.isFinite(date.getTime())
    ? "已解密核验通过　" + date.toLocaleString("zh-CN", {hour12:false})
    : "已有备份通过核验，首次时间未记录";
}

function renderBinding() {
  const registered = binding?.state === "registered";
  const empty = binding?.state === "unregistered";
  const label = element("badge");
  label.textContent = registered ? "已登记" : empty ? "未登记" : binding ? "暂时无法核实" : "读取中";
  label.classList.toggle("registered", registered);
  element("intro").hidden = !empty;
  element("facts").hidden = !registered;
  const publicKey = registered ? binding.recipient : "";
  element("recipient").textContent = publicKey ? publicKey.slice(0,8) + "…" + publicKey.slice(-8) : "";
  element("filename").textContent = registered ? binding.filename || "未记录" : "";
  element("registered-actions").hidden = !registered;
  element("choices").hidden = !empty || !element("selection").hidden || busy;
  element("first-text").textContent = binding ? verifiedText(binding.first) : "正在读取…";
  element("first").classList.toggle("verified", binding?.first?.state === "verified");
  element("first-action").hidden = !registered || binding.first?.state !== "none";
  const current = registered && binding.current_key_backup;
  element("current-proof").hidden = !current;
  element("current-proof").classList.toggle("verified", current?.state === "verified");
  element("current-text").textContent = current ? verifiedText(current) : "";
  lifecycle.setRegisteredRecipient(registered ? publicKey : null, binding?.state);
}

async function refreshBinding(session = owner) {
  if (!session || busy) return;
  const sequence = ++requestNumber;
  let value;
  try {
    const response = await session.request("key-enrollment/binding");
    if (owner !== session || sequence !== requestNumber) return;
    if ([401,403].includes(response.status)) { session.expired(); return; }
    if (!response.ok) throw new Error();
    value = await response.json();
    if (value.project !== session.project || value.instance !== session.instance ||
        !["unknown","registered","unregistered"].includes(value.state) ||
        (value.state === "registered" && !/^age1[a-z0-9]{58}$/.test(value.recipient))) throw new Error();
  } catch {
    value = {state:"unknown"};
  }
  if (owner !== session || sequence !== requestNumber || busy) return;
  if (justEnrolled) {
    // A fresh receipt can precede the worker's next host observation. Never reopen first enrollment on an older sample.
    if (value.state === "unregistered" && value.sampled_at === justEnrolled.priorSample) return;
    if (value.state === "registered") justEnrolled = null;
  }
  if (value.state === "registered" && !element("selection").hidden && !operation) clear();
  binding = value;
  renderBinding();
}

element("copy").addEventListener("click", async () => {
  if (binding?.state !== "registered") return;
  const session = owner, recipient = binding.recipient;
  try {
    await navigator.clipboard.writeText(recipient);
    if (owner === session) feedback.textContent = "已复制完整公钥。";
  } catch { if (owner === session) feedback.textContent = "本站完整公钥：" + recipient; }
});
element("check").addEventListener("click", () => {
  if (binding?.state !== "registered") return;
  checkFile.value = "";
  checkFile.click();
});
checkFile.addEventListener("change", async () => {
  const selected = checkFile.files[0], session = owner;
  if (!session || !selected) return;
  try {
    const {recipient} = await readIdentity(selected, await loadAge());
    await refreshBinding(session);
    if (session !== owner) return;
    feedback.textContent = binding?.state !== "registered" ? "暂时无法核实本站当前登记的钥匙。"
      : recipient === binding.recipient ? "这份本地钥匙与本站匹配。" : "这份本地钥匙与本站不匹配。";
  } catch (error) {
    if (session === owner) feedback.textContent = error instanceof KeyError ? error.message : "无法读取所选钥匙。";
  } finally { checkFile.value = ""; }
});
window.addEventListener("focus", () => { if (owner && !document.hidden) refreshBinding(); });

function clear() {
  generation++;
  busy = false;
  operation = null;
  generatedRecipient = null;
  downloadClicked = false;
  if (download.hasAttribute("href")) URL.revokeObjectURL(download.href);
  download.removeAttribute("href");
  download.hidden = true;
  file.value = "";
  file.disabled = false;
  retry.hidden = true;
  element("selection").hidden = true;
  element("choices").hidden = binding?.state !== "unregistered";
  element("cancel").hidden = false;
  element("confirm").disabled = false;
  feedback.textContent = "";
}

export function setOwner(session) {
  clearInterval(sampleTimer);
  requestNumber++;
  binding = null;
  justEnrolled = null;
  clear();
  checkFile.value = "";
  owner = session;
  lifecycle.setOwner(session);
  renderBinding();
  panel.hidden = !session;
  if (session) {
    refreshBinding(session);
    showLastResult(session, generation);
    sampleTimer = setInterval(() => { if (!document.hidden) refreshBinding(session); }, 15000);
  }
}

async function showLastResult(session, version) {
  try {
    const response = await session.request("key-enrollment");
    if (!response.ok) return;
    const previous = await response.json();
    if (version !== generation || previous.project !== session.project) return;
    if (["queued", "awaiting_packet", "verifying"].includes(previous.state)) {
      feedback.textContent = "本站有一个登记任务正在进行。可回原标签页继续。若原页已关闭，请等待任务结束后再登记。";
    } else if (previous.state === "needs_review") {
      feedback.textContent = stopped.needs_review;
    }
  } catch {
    // The last record is informational; an explicit enrollment rechecks the host before sending a key.
  }
}

function selectFile(fresh) {
  element("choices").hidden = true;
  element("selection").hidden = false;
  element("file-label").textContent = fresh ? "重新选择刚刚保存的新钥匙" : "选择早先保管的备份原钥匙";
  element("file-help").textContent = fresh
    ? "先下载保存，再从保存位置选择同一份文件。以后恢复本站的备份会用到它。"
    : "可以从电脑或 U 盘选择。旧版 identity.txt 和 .agekey 文件均可使用。";
}

element("old").addEventListener("click", () => {
  if (!owner || busy || binding?.state !== "unregistered") return;
  clear();
  selectFile(false);
  file.click();
});

element("new").addEventListener("click", async () => {
  if (!owner || busy || binding?.state !== "unregistered") return;
  clear();
  const version = generation;
  busy = true;
  feedback.textContent = "正在本机生成新钥匙…";
  try {
    const age = await loadAge();
    const secret = await age.generateIdentity();
    const recipient = await age.identityToRecipient(secret);
    const content = new Blob([secret, "\n"], {type:"application/octet-stream"});
    const sum = new Uint8Array(await crypto.subtle.digest("SHA-256", await content.arrayBuffer()));
    const shortHash = Array.from(sum.subarray(0,16), byte => byte.toString(16).padStart(2,"0")).join("");
    if (version !== generation) return;
    generatedRecipient = recipient;
    download.href = URL.createObjectURL(content);
    download.download = "key-" + shortHash + ".agekey";
    download.hidden = false;
    selectFile(true);
    feedback.textContent = "";
    download.focus();
  } catch {
    if (version === generation) feedback.textContent = "未能生成新钥匙，请重新尝试。";
  } finally {
    if (version === generation) busy = false;
  }
});
download.addEventListener("click", () => { downloadClicked = true; });
file.addEventListener("cancel", () => {
  if (!busy && !feedback.textContent) {
    feedback.textContent = file.files.length ? "已保留选中的文件。" : "已取消选文件。可以重新选择，或点取消返回。";
  }
});
file.addEventListener("change", () => { feedback.textContent = ""; });

async function call(path, body, version) {
  if (version !== generation || !owner) throw new Error();
  const response = await owner.request("key-enrollment" + path, body);
  if (version !== generation) throw new Error();
  if ([401,403].includes(response.status)) {
    owner.expired();
    throw new Error();
  }
  if (response.status === 409) throw new KeyError("本站已有登记任务或提交内容与原任务不符。请先查看或等待原任务结束，再尝试登记。");
  if (response.status === 404) throw new KeyError("未找到登记入口或任务，请核对本站版本，或按本站维护说明操作。");
  if ([400,413,422].includes(response.status)) throw new KeyError("本站未接受登记请求，请核对站点和所选原钥匙后重试。");
  if (!response.ok) throw new Error();
  return response.json();
}

element("cancel").addEventListener("click", async () => {
  const id = operation?.id;
  if (operation?.packet) return; // A submitted or uncertain envelope must be checked first.
  clear();
  feedback.textContent = "已取消。可以重新选择登记方式。";
  if (!owner || !id) return;
  const version = generation;
  try { await call("/" + id + "/cancel", {}, version); }
  catch {
    if (version === generation) feedback.textContent = "已退出本页登记。服务器端任务如仍在等待，会自行到期。";
  }
});

const stopped = {
  expired:"本次登记等待超时。请重新选择登记方式再试。",
  cancelled:"本次登记已取消。可以重新选择登记方式。",
  failed:"本站未能完成登记。请查看本站维护说明和任务结果后重试。",
  needs_review:"本次登记需要核对。请先按本站维护说明在服务器查看。",
};
const wait = () => new Promise(resolve => setTimeout(resolve, 1500));

async function follow(op, version, age) {
  let record = await call("", {operation_id:op.id}, version);
  const until = Date.now() + 210000;
  while (version === generation && Date.now() < until) {
    if (record.project !== owner.project || record.operation_id !== op.id) throw new KeyError("站点或任务编号不符，登记已停止。");
    if (Object.hasOwn(stopped, record.state)) throw new KeyError(stopped[record.state]);
    if (record.target) {
      const target = await verifyTarget(record, owner.project);
      if (op.target && (op.target.started_sha256 !== target.started_sha256 || op.target.recipient !== target.recipient)) {
        throw new KeyError("本次交接公钥已改变，登记已停止。");
      }
      op.target = target;
    }
    if (record.state === "succeeded") {
      if (!op.target || (!op.packet && record.registered !== true)) throw new KeyError("缺少本站登记记录，无法核对结果。");
      await verifyReceipt(record.receipt, op.target, op, op.packet?.request);
      return op.packet ? "本站备份钥匙登记通过。" : "已核对，本站原先登记的就是所选的原钥匙。";
    }
    if (record.state === "awaiting_packet" && !op.accepted) {
      if (!op.target || record.registered !== false) throw new KeyError("本站交接状态不完整，登记已停止。");
      if (!op.packet) {
        const key = await readIdentity(file.files[0], age);
        if (key.hash !== op.hash) throw new KeyError("所选文件已变化。请重新选择原钥匙。");
        const packet = await makePacket(age, op.target, op.id, key);
        if (version !== generation) return;
        if (op.filename) packet.filename = op.filename;
        op.packet = packet;
        file.value = "";
        element("cancel").hidden = true;
      }
      feedback.textContent = "正在向本站交付加密的钥匙并核对登记回执…";
      record = await call("/" + op.id + "/packet", op.packet, version);
      op.accepted = true;
      continue;
    }
    if (!["queued", "awaiting_packet", "verifying"].includes(record.state)) throw new KeyError("无法识别登记状态，请按本站维护说明核对。");
    await wait();
    record = await call("/" + op.id, undefined, version);
  }
  throw new Error();
}

async function enroll() {
  if (!owner || busy) return;
  if (!operation && (!file.files.length || (generatedRecipient && !downloadClicked))) {
    feedback.textContent = generatedRecipient ? "请先下载新钥匙，再选择刚刚保存的同一份文件。" : "请先选择备份原钥匙文件。";
    return;
  }
  const version = generation;
  busy = true;
  retry.hidden = true;
  element("confirm").disabled = true;
  try {
    const age = await loadAge();
    if (!operation) {
      const {hash, recipient} = await readIdentity(file.files[0], age);
      if (version !== generation) return;
      if (generatedRecipient && generatedRecipient !== recipient) throw new KeyError("选中的不是刚生成的新钥匙，请选择刚下载的文件。");
      const selected = file.files[0].name;
      const filename = selected && !/[\/\\\p{C}\p{Z}]/u.test(selected.replaceAll(" ", "")) && ![".",".."].includes(selected) &&
        !selected.toUpperCase().includes("AGE-SECRET-KEY-") && new TextEncoder().encode(selected).length <= 255 ? selected : null;
      operation = {id:crypto.randomUUID().replaceAll("-", ""), hash, recipient, filename};
      if (download.hasAttribute("href")) {
        URL.revokeObjectURL(download.href);
        download.removeAttribute("href");
        download.hidden = true;
      }
    }
    file.disabled = true;
    feedback.textContent = "正在核对本站身份和钥匙登记状态…";
    const message = await follow(operation, version, age);
    if (version !== generation) return;
    const instance = operation.target.instance_id;
    justEnrolled = {priorSample:binding?.sampled_at};
    binding = {state:"registered", recipient:operation.recipient, filename:operation.packet ? operation.filename : null,
      first:{state:"unknown"}};
    clear();
    renderBinding();
    refreshBinding();
    feedback.textContent = message + "\n项目 " + owner.project + "，实例 " + instance + "。可运行手动备份。";
  } catch (error) {
    if (version !== generation) return;
    if (error instanceof KeyError) {
      if (operation) clear();
      feedback.textContent = error.message;
    } else {
      feedback.textContent = operation
        ? "暂时无法确认登记状态。可以继续核对同一任务。"
        : "无法读取所选文件或载入钥匙工具，请重新选择后再试。";
      retry.hidden = !operation;
    }
  } finally {
    if (version === generation) {
      busy = false;
      element("confirm").disabled = Boolean(operation);
    }
  }
}
element("confirm").addEventListener("click", enroll);
retry.addEventListener("click", enroll);
