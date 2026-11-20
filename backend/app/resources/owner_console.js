"use strict";

function createOwnerConsole({request, expired}) {
  const el = id => document.getElementById(id);
  const panels = {overview: el("owner-overview"), users: el("owner-users"), tools: el("owner-tools")};
  const nav = el("owner-nav"), dialog = el("owner-about");
  let session = null, view = "overview", overviewVersion = 0, userVersion = 0;
  let users = [], cursor = 0, userBusy = false, aboutOpener = null, aboutScroll = 0, aboutLoad = null;
  const unknown = "尚未取得";
  const stateLabels = {ok:"正常", unavailable:"不可用", unknown:"尚未取得", idle:"尚无备份记录",
    queued:"等待执行", running:"正在备份", succeeded:"已完成", failed:"未完成", needs_review:"需要站主核对"};
  const number = value => Number.isFinite(value) ? value.toLocaleString("zh-CN") : unknown;
  function time(value) {
    if (!value) return unknown;
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? unknown : date.toLocaleString("zh-CN", {hour12:false});
  }
  function space(bytes) {
    if (!Number.isSafeInteger(bytes) || bytes < 0) return unknown;
    const size = bytes >= 2 ** 30 ? 2 ** 30 : 2 ** 20;
    return (bytes / size).toLocaleString("zh-CN", {maximumFractionDigits:1}) + (size === 2 ** 30 ? " GiB" : " MiB");
  }
  function short(value) { return value?.slice(0,8) || unknown; }
  function item(tag, text) {
    const node = document.createElement(tag);
    node.textContent = text;
    return node;
  }
  function facts(id, rows) {
    el(id).replaceChildren(...rows.map(([label,value,warning]) => {
      const row = document.createElement("div"), detail = item("dd",value ?? unknown);
      if (warning) detail.className = "is-warning";
      row.append(item("dt",label),detail);
      return row;
    }));
  }
  async function read(path, owner) {
    const response = await request(owner.token,path);
    if (session !== owner) return null;
    if ([401,403].includes(response.status)) { expired(); return null; }
    if (!response.ok) throw new Error();
    return response.json();
  }
  async function overview() {
    const owner = session, version = ++overviewVersion;
    if (!owner) return;
    el("overview-note").textContent = "正在读取本站状态…";
    try {
      const data = await read("overview",owner);
      if (!data || version !== overviewVersion || session !== owner) return;
      if (data.project !== owner.project || (owner.instance && data.instance !== owner.instance)) throw new Error();
      const {service,database,backup,certificate,storage,totals} = data;
      facts("overview-services", [["网站服务"+(owner.build?.version?" · "+owner.build.version:""),stateLabels[service.web],service.web==="unavailable"],["API",stateLabels[service.api]],
        ["数据库"+(database.version ? " · "+database.version : ""),stateLabels[service.database],service.database==="unavailable"]]);
      facts("overview-backup", [["最近结果",stateLabels[backup.state],["failed","needs_review"].includes(backup.state)],
        ["完成时间",time(backup.checked_at)],["实际解密核验",backup.decryption_verified===true?"已通过":backup.decryption_verified===false?"未通过":"尚无核验结果",backup.decryption_verified===false],
        ["备份编号",backup.backup_id || "—"]]);
      const days = certificate.remaining_days;
      const lowBackupSpace = Number.isFinite(storage.backup_free_bytes) && Number.isFinite(storage.backup_required_bytes)
        && storage.backup_free_bytes < storage.backup_required_bytes;
      facts("overview-resources", [["网站证书",Number.isInteger(days)?(days<0?"已到期":"剩余 "+days+" 天"):unknown,Number.isInteger(days)&&days<=30],
        ["到期时间",time(certificate.expires_at)],
        ["备份可用空间",space(storage.backup_free_bytes)+(lowBackupSpace?" · 低于启动备份所需的最低空间":""),lowBackupSpace]]);
      facts("overview-counts", [["注册用户",number(totals.users)],["联系人",number(totals.contacts)],["事件",number(totals.events)],["记账",number(totals.entries)]]);
      el("overview-note").textContent = "检查时间 "+time(data.checked_at)+" · 服务器采样 "+time(data.sampled_at);
    } catch {
      if (session !== owner || version !== overviewVersion) return;
      for (const id of ["overview-services","overview-counts","overview-backup","overview-resources"]) el(id).replaceChildren();
      el("overview-note").textContent = "暂时无法读取本站状态。可点击刷新，也可在维护操作中查看备份结果。";
    }
  }
  function renderUsers() {
    const needle = el("usage-search").value.trim().toLocaleLowerCase();
    const rows = users.filter(row => !needle || [row.username,row.display_name].some(value => value?.toLocaleLowerCase().includes(needle)));
    const table = document.createDocumentFragment(), cards = document.createDocumentFragment();
    for (const row of rows) {
      const name = row.username + (row.is_active ? "" : "（已停用）");
      const display = row.display_name || "尚未设置", last = row.last_login_at ? time(row.last_login_at) : "尚无记录";
      const tr = document.createElement("tr");
      [name,display,number(row.contacts),number(row.events),number(row.entries),last].forEach(val => tr.append(item("td",val)));
      table.append(tr);
      const card = document.createElement("article"), head = document.createElement("header");
      head.append(item("strong",name),item("span",display));
      card.append(head,item("span","联系人 "+number(row.contacts)+"　事件 "+number(row.events)+"　记账 "+number(row.entries)),item("small","最近登录 · "+last));
      cards.append(card);
    }
    el("usage-rows").replaceChildren(table); el("usage-cards").replaceChildren(cards);
    el("more-users").hidden = cursor === null;
    el("more-users").disabled = userBusy;
    el("users-note").textContent = userBusy ? "正在读取账号…" : rows.length ? "已显示 "+rows.length+" 个账号" :
      (needle ? "未找到匹配的账号。" : "本站尚无已注册的业务账号。");
  }
  async function loadUsers() {
    if (userBusy || cursor === null || !session) return;
    const owner = session, version = userVersion;
    userBusy = true; renderUsers();
    try {
      do {
        const data = await read("usage/"+cursor,owner);
        if (session !== owner || version !== userVersion || !data) return;
        if (!Array.isArray(data.users) || (data.next_after !== null && !(Number.isSafeInteger(data.next_after) && data.next_after>cursor))) throw new Error();
        users.push(...data.users); cursor = data.next_after;
      } while (cursor !== null && el("usage-search").value.trim() && view === "users");
      userBusy = false; renderUsers();
    } catch {
      if (session !== owner || version !== userVersion) return;
      userBusy = false; renderUsers(); el("users-note").textContent = "暂时无法读全业务账号，请点击加载更多重试。";
    }
  }
  function navigate(to) {
    if (!session || !Object.hasOwn(panels,to)) return;
    view = to;
    for (const [id,panel] of Object.entries(panels)) panel.hidden = id !== to;
    for (const btn of nav.querySelectorAll("button")) {
      if (btn.dataset.view===to) btn.setAttribute("aria-current","page");
      else btn.removeAttribute("aria-current");
    }
    window.scrollTo(0,0);
    if (to==="overview") overview();
    if (to==="users" && !users.length) loadUsers();
  }
  async function about(button) {
    if (!session || dialog.open) return;
    const owner = session;
    aboutOpener=button; aboutScroll=window.scrollY;
    const container = el("owner-about-content");
    const controller = new AbortController();
    aboutLoad = controller;
    container.textContent="正在读取本站资料…";
    dialog.showModal();
    try {
      const {loadSiteAbout} = await import("/maintenance/site-info.js");
      if (controller.signal.aborted || session!==owner) return;
      await loadSiteAbout(container,{endpoint:"/api/v1/maintenance/about",token:owner.token,signal:controller.signal});
    } catch {
      if (!controller.signal.aborted && session===owner)
        container.textContent="暂时无法取得本站资料。";
    }
  }
  dialog.addEventListener("close",()=>{
    aboutLoad?.abort(); aboutLoad=null;
    el("owner-about-content").replaceChildren();
    if (aboutOpener?.isConnected && session) {
      aboutOpener.focus({preventScroll:true});
      window.scrollTo(0,aboutScroll);
    }
    aboutOpener=null;
  });
  for (const id of ["about-close","about-x"]) el(id).addEventListener("click",()=>dialog.close());
  nav.addEventListener("click",event=>{
    const button=event.target.closest("button[data-view]");
    if (!button) return;
    if (button.dataset.view==="about") about(button); else navigate(button.dataset.view);
  });
  document.querySelectorAll("[data-jump]").forEach(button=>button.addEventListener("click",()=>{
    const target=button.dataset.jump;
    if (target==="overview") { navigate("overview"); return; }
    navigate(target==="backup"?"tools":"users");
    if (target==="lookup" || target==="reset") {
      const op=el(target==="lookup"?"lookup-users":"choose-reset");
      op.scrollIntoView({block:"center"}); op.focus({preventScroll:true}); op.click();
      return;
    }
    const focus=target==="backup"?el("backup-actions"):target==="accounts"?el("account-help"):null;
    focus?.scrollIntoView({block:"center"}); focus?.querySelector("button")?.focus({preventScroll:true});
  }));
  const lifecycleToggle = el("lifecycle-toggle"), lifecycleBody = el("lifecycle-body");
  lifecycleToggle.addEventListener("click", () => {
    lifecycleBody.hidden = !lifecycleBody.hidden;
    lifecycleToggle.setAttribute("aria-expanded", String(!lifecycleBody.hidden));
  });
  el("refresh-overview").addEventListener("click",overview);
  el("more-users").addEventListener("click",loadUsers);
  el("usage-search").addEventListener("input",()=>{renderUsers(); if (el("usage-search").value.trim()) loadUsers();});
  return {
    setOwner(owner) {
      session = owner; ++overviewVersion; ++userVersion;
      aboutLoad?.abort();
      if (dialog.open) dialog.close();
      users=[]; cursor=0; userBusy=false; el("usage-search").value="";
      for (const id of ["overview-note","overview-identity","overview-backup","overview-services","overview-counts","overview-resources",
        "usage-rows","usage-cards","users-note","owner-about-content","site-guide-name"]) el(id).replaceChildren();
      document.body.classList.toggle("signed-in",Boolean(owner)); nav.hidden=!owner;
      for (const id of ["tools-heading","backup-heading","signed-in-security","account-shortcut","certificate-help"]) el(id).hidden=!owner;
      el("owner-security").hidden = Boolean(owner);
      el(owner ? "signed-in-security" : "owner-security").append(el("owner-password"));
      lifecycleBody.hidden = false;
      lifecycleToggle.setAttribute("aria-expanded", "true");
      if (owner) {
        el("site-guide-name").textContent=owner.instance ? "data/"+owner.project+"-"+owner.instance.slice(0,8)+"-本站维护说明.txt" : "在 data 中查找本站维护说明.txt";
        el("overview-identity").textContent=owner.project+" · 本站实例 "+short(owner.instance);
        read("about",owner).then(info=>{
          if (session===owner && info && (!info.instance_id || info.instance_id===owner.instance)) {
            owner.build=info;
            if (view==="overview") {
              const label=el("overview-services").querySelector("dt");
              if (label && info.version) label.textContent="网站服务 · "+info.version;
            }
          }
        }).catch(()=>{});
        navigate("overview");
      } else {
        panels.users.hidden=true; panels.overview.hidden=true; panels.tools.hidden=false;
      }
    },
  };
}
