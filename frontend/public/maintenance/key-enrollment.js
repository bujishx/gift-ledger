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
  element("choices").hidden = false;
  element("cancel").hidden = false;
  element("confirm").disabled = false;
  feedback.textContent = "";
}

export function setOwner(session) {
  clear();
  owner = session;
  lifecycle.setOwner(session);
  panel.hidden = !session;
  if (session) showLastResult(session, generation);
}

async function showLastResult(session, version) {
  try {
    const response = await session.request("key-enrollment");
    if (!response.ok) return;
    const previous = await response.json();
    if (version !== generation || previous.project !== session.project) return;
    if (previous.state === "succeeded") {
      feedback.textContent = "服务器已保存本站的登记记录。要核对手中的原钥匙，请选择“上传旧钥匙登记”。";
    } else if (["queued", "awaiting_packet", "verifying"].includes(previous.state)) {
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
  if (!owner || busy) return;
  clear();
  selectFile(false);
  file.click();
});

element("new").addEventListener("click", async () => {
  if (!owner || busy) return;
  clear();
  const version = generation;
  busy = true;
  feedback.textContent = "正在本机生成新钥匙…";
  try {
    const age = await loadAge();
    const secret = await age.generateIdentity();
    const recipient = await age.identityToRecipient(secret);
    if (version !== generation) return;
    generatedRecipient = recipient;
    download.href = URL.createObjectURL(new Blob([secret, "\n"], {type:"application/octet-stream"}));
    download.download = owner.project + "-备份原钥匙.agekey";
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
      operation = {id:crypto.randomUUID().replaceAll("-", ""), hash, recipient};
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
    clear();
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
