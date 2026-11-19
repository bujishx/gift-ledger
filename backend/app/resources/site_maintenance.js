"use strict";

// Only the business site's display preference is shared; maintenance credentials stay in this tab.
const themeKey = "gift-ledger-display-theme";
const systemTheme = window.matchMedia("(prefers-color-scheme: dark)");
function applySiteTheme() {
  let mode;
  try { mode = window.localStorage.getItem(themeKey); } catch { /* Use system appearance. */ }
  const theme = ["light", "dark"].includes(mode) ? mode : (systemTheme.matches ? "dark" : "light");
  document.documentElement.dataset.theme = theme;
}
applySiteTheme();
systemTheme.addEventListener("change", applySiteTheme);
window.addEventListener("storage", (event) => {
  if (event.key === themeKey || event.key === null) applySiteTheme();
});

const form = document.querySelector("#maintenance");
const tokenField = document.querySelector("#token");
const result = document.querySelector("#result");
document.querySelector("#site").textContent = location.origin;

const messages = {
  idle: "本站还没有通过维护页提交的备份。",
  queued: "任务已提交。服务器正在等待执行。",
  running: "备份正在执行。网站恢复后，请再查看结果。",
  succeeded: "备份已实际解密核验，服务已回到备份前的状态。",
  failed: "备份未完成。请按本站维护说明在服务器查看原因。",
  needs_review: "上次备份需要站主核对。请按本站维护说明在服务器查看后再继续。",
};

// Keep an uncertain submission's ID for a retry in this tab; never persist the owner credential.
let submission;
const downloadBackup = document.querySelector("#download-backup");
let verifiedBundle = null;
let activeDownload = null;
function clearDownload() {
  activeDownload?.abort();
  activeDownload = null;
  downloadBackup.disabled = false;
  verifiedBundle = null;
  downloadBackup.hidden = true;
}
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const credential = accessMode === "legacy" ? tokenField.value.trim() : accountSession;
  if (!credential || form.hidden) return;
  const backup = event.submitter?.value === "backup";
  const buttons = form.querySelectorAll("button");
  buttons.forEach((button) => { button.disabled = true; });
  clearDownload();
  result.textContent = backup ? "正在向本站提交备份任务…" : "正在查看本站备份结果…";
  try {
    const readStatus = () => ownerRequest(credential, "status");
    let response;
    if (backup && !submission) {
      const previous = await readStatus();
      if (accessMode === "owner" && credential !== accountSession) return;
      if (previous.ok) {
        const current = await previous.clone().json();
        if (["idle", "succeeded", "failed"].includes(current.state)) {
          submission = {
            operation_id: crypto.randomUUID().replaceAll("-", ""),
            after_operation_id: current.operation_id,
          };
        }
      }
      if (!submission) response = previous;
    }
    if (backup && submission) {
      response = await ownerRequest(credential, "backup", submission);
    }
    if (!backup) response = await readStatus();
    const data = await response.json();
    if (accessMode === "owner" && credential !== accountSession) return;
    if (accessMode === "owner" && [401, 403].includes(response.status)) return expiredOwnerSession();
    if (!response.ok && response.status !== 409) {
      result.textContent = typeof data.detail === "string" ? data.detail : "无法读取维护结果，请稍后查看。";
      return;
    }
    const lines = [messages[data.state] || "状态暂时无法确认，请按本站维护说明在服务器查看。"];
    if (data.project) lines.push("项目 " + data.project);
    if (data.operation_id) lines.push("任务编号 " + data.operation_id);
    if (data.backup_id) lines.push("备份编号 " + data.backup_id);
    if (accessMode === "owner" && data.state === "succeeded" && data.decryption_verified) {
      if (data.download_bytes > 0 && /^[a-f0-9]{32}$/.test(data.backup_id)) {
        verifiedBundle = data.backup_id;
        downloadBackup.hidden = false;
      } else if (data.download_unavailable) {
        lines.push("服务器未能生成下载 ZIP。可以在站点目录的 backups/artifacts/ 下复制本次编号的整份备份。");
      }
    }
    if (data.code) lines.push("原因 " + data.code);
    result.textContent = lines.join("\n");
    if (response.status === 409 ||
        (data.operation_id === submission?.operation_id && ["succeeded", "failed"].includes(data.state))) {
      submission = undefined;
    }
  } catch {
    if (accessMode === "legacy" || credential === accountSession)
      result.textContent = "暂时无法联系本站。网页恢复后请先查看备份结果，或按本站维护说明在服务器查看。";
  } finally {
    buttons.forEach((button) => { button.disabled = false; });
  }
});

const accountHelp = document.querySelector("#account-help");
const accountList = document.querySelector("#account-list");
const accountFeedback = document.querySelector("#account-feedback");
const resetForm = document.querySelector("#reset-account");
const userSelect = document.querySelector("#account-select");
const confirmAccount = document.querySelector("#confirm-account");
const confirmText = document.querySelector("#confirm-account-text");
const password = document.querySelector("#new-password");
const repeatPassword = document.querySelector("#repeat-password");
let accountSession = null;
let accountView = 0;
let keyEnrollment;
let restoreView;

async function showRestore(token, project) {
  try {
    restoreView = await import("/maintenance/site-restore.js");
    if (accountSession === token) restoreView.setOwner({
      project,
      request: (path, body, signal) => ownerRequest(token, path, body, signal),
      sendChunk: (path, data, signal) => fetch("/api/v1/maintenance/" + path, {
        method: "POST", headers: {Authorization: "Bearer " + token, "Content-Type": "application/octet-stream"},
        body: data, signal, credentials: "omit", cache: "no-store", redirect: "error",
      }),
      expired: expiredOwnerSession,
    });
  } catch {
    if (accountSession === token) {
      document.querySelector("#restore-inputs").hidden = true;
      document.querySelector("#restore-feedback").textContent = "网页恢复工具暂时无法载入，请刷新重试。";
      document.querySelector("#site-restore").hidden = false;
    }
  }
}

async function showKeyEnrollment(token, project) {
  try {
    keyEnrollment = await import("/maintenance/key-enrollment.js");
    if (accountSession === token) keyEnrollment.setOwner({
      project,
      request: (path, body) => ownerRequest(token, path, body),
      expired: expiredOwnerSession,
    });
  } catch {
    if (accountSession === token) {
      document.querySelector("#key-feedback").textContent =
        "暂时无法载入钥匙登记工具，请刷新页面后重新登录，或按本站维护说明操作。";
      document.querySelector("#key-choices").hidden = true;
      document.querySelector("#key-enrollment").hidden = false;
    }
  }
}

function clearAccountView() {
  accountView++;
  accountHelp.querySelectorAll("button, input, select").forEach((control) => { control.disabled = false; });
  resetForm.reset();
  resetForm.hidden = true;
  userSelect.replaceChildren();
  confirmAccount.checked = false;
  confirmText.textContent = "请先选择要重置密码的业务账号。";
  accountList.replaceChildren();
  accountList.hidden = true;
  accountFeedback.textContent = "";
}

function ownerRequest(token, path, body, signal) {
  const headers = token ? {Authorization: "Bearer " + token} : {};
  const options = {headers, signal, cache: "no-store", credentials: "omit", redirect: "error"};
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    options.method = "POST";
    options.body = JSON.stringify(body);
  }
  return fetch("/api/v1/maintenance/" + path, options);
}

// The site-owner sign-in flow calls this after login and with null on logout.
// No business cookie, backup authorization code, or value read from storage can open this panel.
async function setMaintenanceOwnerSession(token) {
  clearDownload();
  accountSession = null;
  keyEnrollment?.setOwner(null);
  restoreView?.setOwner(null);
  document.querySelector("#site-restore").hidden = true;
  document.querySelector("#key-enrollment").hidden = true;
  accountHelp.hidden = true;
  clearAccountView();
  updateOwnerView();
  if (!token) return false;
  const view = accountView;
  try {
    const response = await ownerRequest(token, "session");
    if (!response.ok) return false;
    const data = await response.json();
    if (view !== accountView || data.authenticated !== true || data.role !== "site_owner" ||
        typeof data.project !== "string" || !data.project) return false;
    accountSession = token;
    showKeyEnrollment(token, data.project);
    showRestore(token, data.project);
    accountHelp.hidden = false;
    ownerProject.textContent = "本站 " + data.project;
    updateOwnerView();
    return true;
  } catch {
    return false;
  }
}

function expiredOwnerSession() {
  setMaintenanceOwnerSession(null);
  ownerFeedback.textContent = "站主登录已失效，请重新登录本站维护页。";
}

async function showAccounts(forReset) {
  clearAccountView();
  if (!accountSession) return;
  const view = accountView;
  accountFeedback.textContent = "正在读取本站业务账号…";
  try {
    const response = await ownerRequest(accountSession, "accounts");
    if (view !== accountView) return;
    if ([401, 403].includes(response.status)) return expiredOwnerSession();
    if (!response.ok) throw Error();
    const {accounts} = await response.json();
    if (view !== accountView) return;
    if (!Array.isArray(accounts) || !accounts.every((a) => a && typeof a.username === "string" &&
        a.username && typeof a.is_active === "boolean" && typeof a.is_admin === "boolean")) throw Error();
    if (!accounts.length) {
      accountFeedback.textContent = "本站还没有业务账号。";
      return;
    }
    const items = document.createDocumentFragment();
    if (forReset) items.append(new Option("请选择登录名", ""));
    for (const account of accounts) {
      const flags = (account.is_active ? "启用" : "停用") + " · " +
        (account.is_admin ? "业务管理员" : "记账用户");
      if (forReset) {
        items.append(new Option(account.username + "（" + flags + "）", account.username));
      } else {
        const row = document.createElement("li");
        const name = document.createElement("strong");
        const detail = document.createElement("small");
        name.textContent = account.username;
        detail.textContent = flags;
        row.append(name, detail);
        items.append(row);
      }
    }
    if (forReset) {
      userSelect.replaceChildren(items);
      resetForm.hidden = false;
      userSelect.focus();
      accountFeedback.textContent = "";
    } else {
      accountList.replaceChildren(items);
      accountList.hidden = false;
      accountFeedback.textContent = "本站共有 " + accounts.length + " 个业务账号。";
    }
  } catch {
    if (view === accountView) accountFeedback.textContent = "暂时无法读取账号。可按本站维护说明使用服务器命令查找登录名。";
  }
}

document.querySelector("#lookup-users").addEventListener("click", () => showAccounts(false));
document.querySelector("#choose-reset").addEventListener("click", () => showAccounts(true));
document.querySelector("#cancel-reset").addEventListener("click", clearAccountView);
userSelect.addEventListener("change", () => {
  password.value = "";
  repeatPassword.value = "";
  confirmAccount.checked = false;
  confirmText.textContent = userSelect.value
    ? "我已核实本人，确认重置「" + userSelect.value + "」的密码。"
    : "请先选择要重置密码的业务账号。";
});
resetForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!accountSession || resetForm.hidden || !resetForm.reportValidity()) return;
  if (password.value !== repeatPassword.value) {
    accountFeedback.textContent = "两次输入的新密码不同，请重新核对。";
    repeatPassword.focus();
    return;
  }
  const target = userSelect.value;
  const view = accountView;
  const controls = [...accountHelp.querySelectorAll("button, input, select")];
  controls.forEach((control) => { control.disabled = true; });
  accountFeedback.textContent = "正在为「" + target + "」重置密码…";
  try {
    const request = ownerRequest(accountSession, "accounts/reset-password", {
      username: target, new_password: password.value,
    });
    password.value = "";
    repeatPassword.value = "";
    confirmAccount.checked = false;
    const response = await request;
    if (view !== accountView) return;
    if ([401, 403].includes(response.status)) return expiredOwnerSession();
    if (response.ok) {
      clearAccountView();
      accountFeedback.textContent = "「" + target + "」的密码已重置。请让本人用原登录名和新密码登录。";
    } else if ([400, 422].includes(response.status)) {
      accountFeedback.textContent = "新密码未通过校验，请重新输入符合本站要求的密码。";
    } else if (response.status === 404) {
      accountFeedback.textContent = "这个业务账号或密码重置入口当前不可用，请重新查看登录名。";
    } else {
      throw Error();
    }
  } catch {
    if (view === accountView) accountFeedback.textContent = "未能确认重置结果，请先与本人核对新密码能否登录。";
  } finally {
    if (view === accountView) controls.forEach((control) => { control.disabled = false; });
  }
});
const ownerAccess = document.querySelector("#owner-access");
const ownerLogin = document.querySelector("#owner-login");
const ownerMethod = document.querySelector("#owner-method");
const ownerCredential = document.querySelector("#owner-credential");
const ownerFeedback = document.querySelector("#owner-feedback");
const ownerProject = document.querySelector("#owner-project");
const ownerSecurity = document.querySelector("#owner-security");
const ownerPasswordForm = document.querySelector("#owner-password");
const passwordAction = document.querySelector("#owner-password-action");
const settingCode = document.querySelector("#owner-setting-code");
const ownerNew = document.querySelector("#owner-new-password");
const ownerRepeat = document.querySelector("#owner-repeat-password");
const retryAccess = document.querySelector("#retry-access");
let accessMode = "loading";
let loginBusy = false;
let loginVersion = 0;

function updateOwnerView() {
  const legacy = accessMode === "legacy";
  ownerAccess.hidden = accessMode !== "owner";
  form.hidden = !(legacy || (accessMode === "owner" && accountSession));
  tokenField.disabled = !legacy;
  document.querySelector("#legacy-owner-field").hidden = !legacy;
  ownerLogin.hidden = Boolean(accountSession);
  document.querySelector("#owner-current").hidden = !accountSession;
  if (!accountSession) {
    ownerProject.textContent = "";
    result.textContent = "";
    submission = undefined;
  }
}

async function loadAccessMethods() {
  accessMode = "loading";
  updateOwnerView();
  retryAccess.hidden = true;
  ownerFeedback.textContent = "正在读取本站登录方式…";
  try {
    const response = await ownerRequest(null, "access-methods");
    if (response.status === 404) {
      accessMode = "legacy";
    } else {
      if (!response.ok) throw Error();
      const data = await response.json();
      if (data.default !== "code" || data.username_required !== false ||
          !Array.isArray(data.methods) || !data.methods.includes("code") ||
          !data.methods.includes("password")) throw Error();
      accessMode = "owner";
    }
    ownerFeedback.textContent = "";
  } catch {
    accessMode = "unavailable";
    ownerFeedback.textContent = "暂时无法读取本站登录方式。请稍后重试，或按本站维护说明从服务器查看。";
    retryAccess.hidden = false;
  }
  updateOwnerView();
}

function updateLoginMethod() {
  ownerCredential.value = "";
  const code = ownerMethod.value === "code";
  document.querySelector("#credential-label").textContent = code ? "本站短时码" : "站主密码";
  document.querySelector("#login-help").textContent = code
    ? "请在服务器上打开本站的 data/本站维护说明.txt，按说明取得短时码。"
    : "输入为本站设置的站主密码。忘记密码时，可用服务器新签发的短时码重新设置。";
}

function updatePasswordAction() {
  const disable = passwordAction.value === "disable";
  ownerNew.value = "";
  ownerRepeat.value = "";
  settingCode.value = "";
  ownerNew.disabled = disable;
  ownerRepeat.disabled = disable;
  document.querySelector("#owner-password-fields").hidden = disable;
  document.querySelector("#owner-set-password").textContent = disable
    ? "确认关闭站主密码登录" : "保存新站主密码";
}

async function signIn(path, body, successMessage) {
  if (accessMode !== "owner" || loginBusy) return;
  const version = ++loginVersion;
  loginBusy = true;
  const buttons = ownerAccess.querySelectorAll("button");
  buttons.forEach((button) => { button.disabled = true; });
  ownerFeedback.textContent = "正在向本站验证…";
  try {
    const response = await ownerRequest(null, path, body);
    if (version !== loginVersion) return;
    if (!response.ok) {
      const errors = {
        401: "验证失败，请核对本站短时码或站主密码。",
        403: "验证失败，请核对本站地址和凭据。已用过或到期的短时码需要重新签发。",
        422: "输入未通过校验，请核对短时码和密码要求。",
        429: "尝试次数较多，请稍后再试。",
      };
      ownerFeedback.textContent = errors[response.status] || "本站暂时无法验证，请稍后重试。";
      return;
    }
    const data = await response.json();
    if (version !== loginVersion) return;
    if (data.role !== "site_owner" || typeof data.token !== "string" || !data.token ||
        !await setMaintenanceOwnerSession(data.token)) throw Error();
    if (version !== loginVersion) return;
    ownerPasswordForm.reset();
    updatePasswordAction();
    ownerSecurity.open = false;
    ownerFeedback.textContent = successMessage;
  } catch {
    if (version === loginVersion) {
      ownerFeedback.textContent = "未能确认站主会话。请重新登录，已使用的短时码需在服务器重新签发。";
    }
  } finally {
    if (version === loginVersion) {
      loginBusy = false;
      buttons.forEach((button) => { button.disabled = false; });
    }
  }
}

ownerMethod.addEventListener("change", updateLoginMethod);
passwordAction.addEventListener("change", updatePasswordAction);
retryAccess.addEventListener("click", loadAccessMethods);
ownerLogin.addEventListener("submit", (event) => {
  event.preventDefault();
  if (loginBusy || !ownerLogin.reportValidity()) return;
  const method = ownerMethod.value;
  const credential = method === "code" ? ownerCredential.value.trim() : ownerCredential.value;
  ownerCredential.value = "";
  signIn("login", {method, credential}, "");
});
ownerPasswordForm.addEventListener("submit", (event) => {
  event.preventDefault();
  if (loginBusy || !ownerPasswordForm.reportValidity()) return;
  const disable = passwordAction.value === "disable";
  if (!disable && ownerNew.value !== ownerRepeat.value) {
    ownerFeedback.textContent = "两次输入的新站主密码不同，请重新核对。";
    ownerRepeat.focus();
    return;
  }
  const body = {code: settingCode.value.trim(), new_password: disable ? null : ownerNew.value};
  settingCode.value = "";
  ownerNew.value = "";
  ownerRepeat.value = "";
  signIn("owner-password", body, disable ? "已关闭密码登录，可用短时码登录本站。" : "本站的站主密码已保存。");
});
document.querySelector("#owner-logout").addEventListener("click", async () => {
  const token = accountSession;
  if (!token) return;
  const version = ++loginVersion;
  await setMaintenanceOwnerSession(null);
  ownerFeedback.textContent = "正在退出站主登录…";
  try {
    const response = await ownerRequest(token, "logout", {});
    if (version === loginVersion)
      ownerFeedback.textContent = response.ok ? "已退出站主登录。" : "本页会话已清除，服务器未确认退出。";
  } catch {
    if (version === loginVersion) ownerFeedback.textContent = "本页会话已清除，服务器未确认退出。";
  }
});
window.addEventListener("pagehide", () => {
  ++loginVersion;
  loginBusy = false;
  ownerAccess.querySelectorAll("button").forEach((button) => { button.disabled = false; });
  ownerLogin.reset();
  updateLoginMethod();
  tokenField.value = "";
  ownerPasswordForm.reset();
  updatePasswordAction();
  ownerSecurity.open = false;
  ownerFeedback.textContent = "";
  setMaintenanceOwnerSession(null);
});
loadAccessMethods();

downloadBackup.addEventListener("click", async () => {
  const id = verifiedBundle;
  const token = accountSession;
  if (!id || !token || activeDownload) return;
  const controller = new AbortController();
  const signal = controller.signal;
  const current = () => !signal.aborted && activeDownload === controller && token === accountSession && id === verifiedBundle;
  activeDownload = controller;
  downloadBackup.disabled = true;
  let sink;
  try {
    const filename = id + ".zip";
    const picker = typeof window.showSaveFilePicker === "function" ?
      await window.showSaveFilePicker({suggestedName: filename}) : null;
    if (!current()) return;
    const response = await ownerRequest(token, "backups/" + id + "/download", undefined, signal);
    if (!current()) return;
    if (response.status === 401 || response.status === 403) return expiredOwnerSession();
    if (!response.ok) throw new Error();
    if (picker) {
      sink = await picker.createWritable();
      await response.body.pipeTo(sink, {signal});
    } else {
      const blob = await response.blob();
      if (!current()) return;
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      link.hidden = true;
      document.body.append(link);
      try { link.click(); } finally {
        link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 60000);
      }
    }
    if (current()) result.textContent = "备份文件 " + filename + " 已交给浏览器保存。请将原钥匙与备份分开保管。";
  } catch (error) {
    if (sink) await sink.abort().catch(() => {});
    if (current() && error.name !== "AbortError") {
      result.textContent = "下载未完成。可以重新下载，或在本站 backups/artifacts/ 下复制编号为 " + id + " 的目录。";
    }
  } finally {
    if (activeDownload === controller) {
      activeDownload = null;
      downloadBackup.disabled = false;
    }
  }
});
