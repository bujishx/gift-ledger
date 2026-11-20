import {KeyError, loadAge, makePacket, readIdentity, verifyTarget} from "./age-transfer.js";

const el = id => document.getElementById("restore-" + id);
const panel = document.getElementById("site-restore");
const chunkSize = 4 * 1024 * 1024;
const active = ["uploading", "queued", "awaiting_packet", "preflighting", "eligible", "applying"];
const notes = {
  idle:"本站还没有网页恢复任务。",
  uploading:"正在接收旧站备份。中途关闭页面后，可重新选择同一份备份继续。",
  queued:"备份已送达，正在等待目标站准备临时钥匙。",
  awaiting_packet:"请选择创建这份旧备份时对应的原钥匙。本站只在本次恢复期间使用它。",
  preflighting:"正在检查目标是否为空，并实际解密核验备份。",
  eligible:"预检通过。请核对源站、账目笔数和目标网址，再决定是否恢复。",
  applying:"已开始恢复。网站可能短暂离线，重新打开本站后可查看进度。",
  succeeded:"恢复完成，已核验数据库和应用文件，并清除了本次恢复使用的临时钥匙。",
  failed:"本次恢复未完成，请按本站维护说明查看原因。",
  cancelled:"本次恢复已取消。",
  expired:"本次恢复已到期。可以重新选择旧备份和原钥匙。",
  needs_review:"本次恢复需要站主在服务器核对。请保留原备份，并按本站维护说明查看。",
};
let owner, current, controller, busy = false, candidate, sentPacket, confirmed;

function clearFiles() {
  el("files").value = "";
  el("key").value = "";
}

export function setOwner(next) {
  controller?.abort();
  controller = new AbortController();
  owner = next; current = candidate = sentPacket = confirmed = null;
  busy = false;
  clearFiles();
  el("confirm-text").value = "";
  el("preview").textContent = el("feedback").textContent = "";
  panel.hidden = !next;
  if (next) run(refresh);
}

function selection() {
  const chosen = [...el("files").files];
  if (chosen.length === 1 && /\.zip$/i.test(chosen[0].name) && chosen[0].size > 0) {
    return [{name:"input.zip", size:chosen[0].size, file:chosen[0]}];
  }
  const names = new Set(chosen.map(f => f.name));
  if (chosen.length !== 5 || names.size !== 5 || !["BUNDLE.json", "backup.json", "verify.json", "SHA256SUMS"].every(n => names.has(n)) ||
      !chosen.some(f => /^backup-[a-f0-9]{32}\.glgui\.age$/.test(f.name)) || chosen.some(f => !f.size)) {
    throw new KeyError("请选择一个旧备份 ZIP，或一次选齐同一备份目录中的原始五个文件。");
  }
  return chosen.map(file => ({name:file.name, size:file.size, file})).sort((a,b) => a.name < b.name ? -1 : 1);
}

function displayControls() {
  const state = current?.state;
  const fresh = !state || ["idle", "failed", "cancelled", "expired"].includes(state);
  el("inputs").hidden = !fresh && !["uploading", "awaiting_packet"].includes(state);
  el("start").hidden = el("inputs").hidden;
  el("files").closest("label").hidden = state === "awaiting_packet";
  el("start").textContent = state === "awaiting_packet" ? "提交本次恢复钥匙" :
    state === "uploading" ? "继续上传并预检" : "上传并预检";
  el("start").disabled = busy || !state;
  el("files").disabled = el("key").disabled = busy;
  el("approval").hidden = state !== "eligible" || confirmed === current?.operation_id;
  el("confirm").disabled = busy || el("confirm-text").value !== "恢复";
  el("cancel").hidden = !active.includes(state) || state === "applying" || confirmed === current?.operation_id;
  el("status").disabled = busy;
}

function show(data) {
  if (data.project !== owner.project || !Object.hasOwn(notes,data.state) ||
      !/^[a-f0-9-]{36}$/.test(data.instance_id) || (data.state !== "idle" && !/^[a-f0-9]{32}$/.test(data.operation_id))) {
    throw new KeyError("本站恢复状态无效，请核对维护页网址。");
  }
  const p = data.preview;
  if (p && (p.target_project !== data.project || p.target_instance_id !== data.instance_id ||
      ![location.origin, location.origin + "/"].includes(p.target_url) ||
      !Number.isSafeInteger(p.gift_count) || p.gift_count < 0 || p.source_instance_id === data.instance_id)) {
    throw new KeyError("预检回执的目标站或笔数不符，无法确认恢复。");
  }
  if (current?.operation_id !== data.operation_id) el("confirm-text").value = "";
  if (["failed", "expired", "cancelled"].includes(data.state)) candidate = sentPacket = confirmed = null;
  current = data;
  el("preview").textContent = p ? [
    "源站 " + p.source_project + "，共 " + p.gift_count + " 笔。",
    "目标网址 " + p.target_url, "目标项目 " + p.target_project, "目标实例 " + p.target_instance_id,
  ].join("\n") : "";
  const lines = [notes[data.state]];
  if (data.state === "succeeded") {
    lines.push("已恢复 " + data.result.gift_count + " 笔，金额合计 " + data.result.cash_amount_all_gift_records + " 元。");
    lines.push("登录本站核对原账。今后从本站备份，可选择“上传旧钥匙登记”或“生成新钥匙登记”。");
    clearFiles();
  }
  if (["failed","needs_review"].includes(data.state) && data.code) lines.push("原因 " + data.code);
  if (data.operation_id) lines.push("恢复任务 " + data.operation_id);
  el("feedback").textContent = lines.join("\n");
  displayControls();
}

async function invoke(ctx, path="", body, binary=false) {
  ctx.signal.throwIfAborted();
  const response = binary ? await ctx.owner.sendChunk("restore"+path,body,ctx.signal)
    : await ctx.owner.request("restore"+path,body,ctx.signal);
  ctx.signal.throwIfAborted();
  if ([401,403].includes(response.status)) { ctx.owner.expired(); throw new Error(); }
  if (!response.ok) {
    const messages = {400:"恢复材料或确认内容无效，请重新核对。",404:"本站尚未启用网页恢复，或没有这次恢复记录。",
      409:"本站已有任务或材料已变化。请先查看恢复进度。",413:"上传内容超过此接口允许的大小。",
      507:"目标站空间不足，请在服务器腾出空间后再试。"};
    throw new KeyError(messages[response.status] || "暂时无法联系本站。页面恢复后请先查看进度。");
  }
  const data = await response.json();
  ctx.signal.throwIfAborted();
  return data;
}

async function run(work) {
  if (busy || !owner) return;
  const ctx = {owner,signal:controller.signal};
  busy = true; displayControls();
  try { await work(ctx); }
  catch (error) {
    if (!ctx.signal.aborted) el("feedback").textContent = error instanceof KeyError ? error.message :
      "暂时无法确认恢复进度。重新联系到本站后，请先查看同一任务。";
  } finally {
    if (!ctx.signal.aborted) { busy = false; displayControls(); }
  }
}

async function refresh(ctx) {
  const record = await invoke(ctx);
  show(record);
  return record;
}

async function follow(ctx, record, keyFile) {
  const op = record.operation_id;
  const path = "/" + op;
  const until = Date.now() + 180000;
  while (true) {
    if (record.operation_id !== op) throw new KeyError("本站返回了不同的恢复任务。");
    show(record);
    if (record.state === "awaiting_packet" && (keyFile || sentPacket?.op === op)) {
      if (!sentPacket || sentPacket.op !== op) {
        const age = await loadAge();
        const key = await readIdentity(keyFile,age);
        const target = await verifyTarget(record,ctx.owner.project);
        const packet = await makePacket(age,target,op,key);
        ctx.signal.throwIfAborted();
        sentPacket = {op, packet};
      }
      if (!sentPacket.accepted) {
        await invoke(ctx,path+"/packet",sentPacket.packet);
        sentPacket.accepted = true;
        clearFiles();
      }
    } else if (record.state === "awaiting_packet" || !["queued","preflighting","applying"].includes(record.state)) {
      return;
    }
    if (Date.now() >= until) return;
    await new Promise(resolve => setTimeout(resolve,1000));
    record = await invoke(ctx,path);
  }
}

async function begin(ctx) {
  const key = el("key").files[0];
  if (!key) throw new KeyError("请选择创建这份备份时使用的原钥匙。");
  await readIdentity(key,await loadAge()); // Catch a mistaken choice before transferring the backup.
  if (current?.state === "awaiting_packet") return follow(ctx,current,key);
  const chosen = selection();
  const files = chosen.map(({name,size}) => ({name,size}));
  const op = current?.state === "uploading" ? current.operation_id : (candidate ||= crypto.randomUUID().replaceAll("-",""));
  let record = await invoke(ctx,"",{operation_id:op,files});
  show(record);
  if (record.operation_id !== op) throw new KeyError("恢复任务编号不符，请先查看进度。");
  if (record.state === "uploading") {
    for (const [i, item] of chosen.entries()) {
      for (let offset=0; offset<item.size; offset+=chunkSize) {
        const end = Math.min(item.size, offset+chunkSize);
        el("feedback").textContent = "正在上传 " + item.name + "（" + Math.floor(offset/item.size*100) + "%）…";
        const result = await invoke(ctx,"/"+op+"/files/"+i+"/"+offset,item.file.slice(offset,end),true);
        if (result.received !== end) throw new KeyError("上传进度不符，请重新核对所选备份。");
      }
    }
    record = await invoke(ctx,"/"+op+"/seal",{});
  }
  await follow(ctx,record,key);
}

el("start").addEventListener("click",()=>run(begin));
el("status").addEventListener("click",()=>run(async ctx => {
  const r = await refresh(ctx);
  if (["failed","cancelled","expired","idle"].includes(r.state)) candidate = sentPacket = confirmed = null;
}));
el("confirm-text").addEventListener("input",displayControls);
el("confirm").addEventListener("click",()=>run(async ctx => {
  if (current?.state !== "eligible" || el("confirm-text").value !== "恢复") return;
  const {operation_id:op,preview} = current;
  await invoke(ctx,"/"+op+"/confirm",{operation_id:op,preview,confirmation:"恢复"});
  confirmed = op; el("confirm-text").value = ""; displayControls();
  el("feedback").textContent = "确认已提交。恢复期间网站可能短暂离线，稍后可重新登录查看同一任务。";
  await new Promise(resolve=>setTimeout(resolve,1500));
  const record = await invoke(ctx,"/"+op);
  if (record.state !== "eligible") await follow(ctx,record);
}));
el("cancel").addEventListener("click",()=>{
  if (!owner || !current?.operation_id || confirmed === current.operation_id) return;
  const op = current.operation_id;
  controller.abort();controller = new AbortController();busy = false;
  run(async ctx => {
    const record=await invoke(ctx,"/"+op+"/cancel",{});
    candidate=sentPacket=confirmed=null;
    clearFiles();show(record);
    if (active.includes(record.state)) el("feedback").textContent = "取消请求已提交。请稍后查看任务结果。";
  });
});
