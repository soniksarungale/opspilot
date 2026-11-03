const FACT_FIELDS = ["location", "store", "item", "quantity_required", "quantity_available", "supplier",
  "delay_days", "deadline", "event_date", "requested_action"];
const RED = ["injection", "approval_overdue"];
const GREY = ["duplicate"];
const OPEN = ["processing", "needs_info", "review", "pending_approval", "in_progress"];
let me = null;
let timer = null;

const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const rs = n => "Rs " + Number(n).toLocaleString("en-IN");
const when = iso => iso ? new Date(iso).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "";
const label = s => String(s).replace(/_/g, " ");
const flagClass = code => RED.includes(code) ? "red" : GREY.includes(code) ? "" : "amber";

function showError(message) {
  const banner = document.getElementById("banner");
  banner.textContent = message + " (click to dismiss)";
  banner.hidden = false;
}
document.getElementById("banner").onclick = e => { e.currentTarget.hidden = true; };

async function api(path, body, method) {
  const opts = { method: method || (body !== undefined ? "POST" : "GET"), headers: { "Content-Type": "application/json" } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(data.detail) ? "Some input is invalid" : data.detail;
    if (res.status === 401 && me) { me = null; render(); }
    showError(detail || "Request failed");
    throw new Error(detail);
  }
  document.getElementById("banner").hidden = true;
  return data;
}

function badge(level) {
  return level ? `<span class="risk risk-${esc(level)}">${esc(level)}</span>` : '<span class="muted">-</span>';
}
const pill = status => status === "processing" ? '<span class="status status-processing"><span class="spin"></span> analysing</span>'
  : `<span class="status status-${esc(status)}">${esc(label(status))}</span>`;
const STEPS = ["received", "review", "pending_approval", "in_progress", "resolved", "closed"];
function stepper(status) {
  if (status === "cancelled") return "";
  const at = { processing: 0, needs_info: 1 }[status] ?? STEPS.indexOf(status);
  return `<ol class="steps">${STEPS.map((s, i) => `<li class="${i < at ? "done" : i === at ? "now" : ""}">${
    i === 1 && status === "needs_info" ? "needs info" : label(s)}</li>`).join("")}</ol>`;
}
function meter(score) {
  if (score == null) return "";
  return `<div class="meter ${score < 0.6 ? "low" : score < 0.8 ? "mid" : ""}"><i style="width:${Math.round(score * 100)}%"></i></div>`;
}
function flagTags(codes) {
  return codes.map(c => `<span class="tag ${flagClass(c)}">${esc(label(c))}</span>`).join("");
}

function loginView() {
  return `<div class="login box"><h1>OpsPilot</h1><p class="muted">Operations requests, from inbox to closure: extracted, checked,
    assessed and routed for approval, with every agent and human step on record.</p>
    <form id="login"><div class="row"><label>Username</label><input name="username" autocomplete="username"></div>
    <div class="row"><label>Password</label><input name="password" type="password" autocomplete="current-password"></div>
    <button class="primary">Log in</button></form>
    <h2>Demo accounts</h2>
    <table><tr><th>User</th><th>Role</th><th>Can approve up to</th></tr>
    <tr><td>analyst</td><td>analyst</td><td>nothing</td></tr>
    <tr><td>manager</td><td>manager</td><td>Rs 50,000</td></tr>
    <tr><td>head</td><td>regional head</td><td>Rs 5,00,000</td></tr>
    <tr><td>admin</td><td>admin</td><td>any amount</td></tr></table>
    <p class="muted">Password for all: demo1234</p></div>`;
}

function shell(content, count) {
  const route = location.hash.split("/")[1] || "requests";
  const link = (r, text) => `<a href="#/${r}" class="${route === r ? "on" : ""}">${text}</a>`;
  return `<header><span class="brand">OpsPilot</span><nav>
    ${link("requests", "Requests")}${link("inbox", "Inbox")}${link("approvals", "Approvals" + (count ? `<span class="count">${count}</span>` : ""))}
    ${link("report", "Report")}${me.role === "admin" ? link("admin", "Admin") : ""}</nav>
    <span class="who">${esc(me.name)} (${esc(label(me.role))})</span><button data-act="logout">Logout</button></header>
    <main>${content}</main>`;
}

async function requestsView() {
  const rows = await api("/api/requests");
  if (rows.some(r => r.status === "processing")) timer = setTimeout(render, 3000);
  const body = rows.map(r => `<tr><td>${r.id}</td><td><a href="#/requests/${r.id}">${esc(r.subject)}</a></td>
    <td>${pill(r.status)}</td><td>${badge(r.risk)}</td><td>${r.confidence ?? ""}</td>
    <td>${esc(when(r.deadline))}</td><td>${flagTags(r.flags)}</td></tr>`).join("");
  const open = rows.filter(r => OPEN.includes(r.status));
  const stat = (n, text) => `<div class="stat"><b>${n}</b><span>${text}</span></div>`;
  return `<h1>Requests</h1><div class="stats">${stat(open.length, "Open")}${stat(rows.filter(r => r.status === "pending_approval").length, "Awaiting approval")}
    ${stat(open.filter(r => ["high", "critical"].includes(r.risk)).length, "High or critical risk")}${stat(rows.filter(r => r.status === "needs_info").length, "Need information")}</div><div class="wrap"><table><tr><th>#</th><th>Subject</th><th>Status</th><th>Risk</th>
    <th>Confidence</th><th>Deadline</th><th>Flags</th></tr>${body || '<tr><td colspan="7" class="muted">No requests yet. Import one from the inbox.</td></tr>'}</table></div>`;
}

async function inboxView() {
  const mails = await api("/api/inbox");
  const body = mails.map(m => `<tr><td>${esc(when(m.received_at))}</td><td>${esc(m.sender)}</td><td>${esc(m.subject)}</td>
    <td>${m.request_id ? `<a href="#/requests/${m.request_id}">Request #${m.request_id}</a>`
      : `<button class="primary" data-act="import" data-id="${m.id}">Import</button>`}</td></tr>`).join("");
  return `<h1>Inbox</h1><div class="wrap"><table><tr><th>Received</th><th>From</th><th>Subject</th><th></th></tr>${body}</table></div>
    <h2>New request</h2><form id="new-request" class="box"><div class="row"><label>Subject</label><input name="subject" size="50" required maxlength="200"></div>
    <div class="row"><label>Sender</label><input name="sender" size="50" required maxlength="200"></div>
    <div class="row"><label>Details</label><textarea name="body" required maxlength="20000"></textarea></div><button class="primary">Submit</button></form>`;
}

function factRows(req) {
  return FACT_FIELDS.map(f => {
    const ev = req.evidence[f];
    const note = ev && ev.by ? `set by ${esc(ev.by)}` : ev ? `"${esc(ev)}"` : "";
    const value = req.facts[f];
    const shown = f === "deadline" || f === "event_date" ? when(value) || value : value;
    return `<tr><td>${esc(label(f))}</td><td>${value == null ? '<span class="muted">not stated</span>' : esc(shown)}</td>
      <td class="muted">${note}</td><td>${["needs_info", "review"].includes(req.status) ? `<button data-act="edit" data-field="${f}">Edit</button>` : ""}</td></tr>`;
  }).join("");
}

function optionCard(o, i, req) {
  const low = req.flags.some(f => f.code === "low_confidence");
  const tags = (o.recommended && !low ? '<span class="tag green">Recommended</span>' : "") + (o.rejected ? '<span class="tag red">Rejected</span>' : "");
  const choose = req.status === "review" && !o.rejected ? `<button class="${o.recommended && !low ? "primary" : ""}" data-act="choose" data-i="${i}">Choose</button>` : "";
  return `<div class="option ${o.rejected ? "rejected" : o.recommended && !low ? "recommended" : ""}"><h3>${esc(o.title)} ${tags}</h3>
    <div>Extra cost: <b>${rs(o.est_cost_inr)}</b> ${o.time_to_effect ? `| ${esc(o.time_to_effect)}` : ""}</div>
    ${(o.actions || []).length ? `<ul>${o.actions.map(a => `<li>${esc(a)}</li>`).join("")}</ul>` : ""}
    <div>${esc(o.rationale)}</div>${o.risks ? `<div class="muted">Risks: ${esc(o.risks)}</div>` : ""}<div>${choose}</div></div>`;
}

function approvalRows(req) {
  return req.approvals.map(a => `<tr><td>${esc(label(a.role))}</td><td>${rs(a.amount)}</td><td>${esc(when(a.due_at))}</td>
    <td>${pill(a.status)}</td><td>${a.status === "pending" ? `<button data-act="decide" data-id="${a.id}" data-approve="1">Approve</button>
    <button class="danger" data-act="decide" data-id="${a.id}" data-approve="0">Reject</button>` : esc(a.reason || "")}</td></tr>`).join("");
}

function auditRows(req) {
  return req.audit.map(a => {
    const who = a.actor_type === "agent" ? `<i>${esc(a.actor)}</i>` : esc(a.actor);
    const detail = Object.entries(a.detail).filter(([, v]) => v !== "" && v !== null && !(Array.isArray(v) && !v.length))
      .map(([k, v]) => `${label(k)}: ${Array.isArray(v) ? v.join(", ") : typeof v === "object" ? JSON.stringify(v) : v}`).join(" · ");
    return `<tr><td class="nowrap">${esc(when(a.ts))}</td><td>${who}</td><td class="nowrap">${esc(label(a.action))}</td><td class="detail">${esc(detail)}</td></tr>`;
  }).join("");
}

async function detailView(id) {
  const req = await api(`/api/requests/${id}`);
  if (req.status === "processing") timer = setTimeout(render, 3000);
  const risk = req.risk || {};
  const lowConf = req.flags.some(f => f.code === "low_confidence");
  const can = a => ({ resolve: req.status === "in_progress", close: req.status === "resolved", cancel: OPEN.includes(req.status) })[a];
  const buttons = ["resolve", "close", "cancel"].filter(can).map(a => `<button data-act="status" data-action="${a}" class="${a === "cancel" ? "danger" : ""}">${a[0].toUpperCase() + a.slice(1)}</button>`).join(" ");
  const options = req.options.map((o, i) => optionCard(o, i, req)).join("");
  const messages = req.messages.map(m => `<div class="box"><b>${esc(m.subject)}</b> <span class="muted">to ${esc(m.recipient)} (${esc(m.status)})</span>
    <pre class="text">${esc(m.body)}</pre>${m.status === "failed" ? '<div class="flag red">Not delivered: the mail server was unavailable</div>' : ""}
    ${m.status === "draft" || m.status === "failed" ? `<button data-act="send" data-id="${m.id}">${m.status === "failed" ? "Retry send" : "Send"}</button>` : ""}</div>`).join("");
  return `<p><a href="#/requests">&larr; Requests</a></p><h1>#${req.id} ${esc(req.subject)} ${pill(req.status)}</h1>${stepper(req.status)}
  <div class="cols"><div>
    <div class="box"><div class="muted">From ${esc(req.sender)} via ${esc(req.channel)}, ${esc(when(req.received_at))}</div><pre class="mail">${esc(req.body)}</pre></div>
    <h2>Flags</h2>${req.flags.map(f => `<div class="flag ${flagClass(f.code)}">${esc(f.message)}</div>`).join("") || '<div class="muted">None</div>'}
    <h2>Facts</h2><div class="wrap"><table><tr><th>Field</th><th>Value</th><th>Evidence</th><th></th></tr>${factRows(req)}</table></div>
    <h2>Supplier update</h2><form id="supplier" class="box"><div class="row"><label>New delay (days)</label><input name="delay_days" type="number" min="0" required></div>
      <div class="row"><label>Note</label><input name="note" size="40"></div><button>Apply</button></form>
  </div><div>
    <h2>Risk</h2><div class="box">Agent: ${badge(risk.risk_level)} | Rules: ${badge(risk.rule_level)} | Final: ${badge(risk.final_level)}
      ${risk.impact ? `<div>${esc(risk.impact)}</div>` : ""}<ul>${(risk.reasons || []).map(r => `<li>${esc(r)}</li>`).join("")}</ul>
      <div style="margin-top:8px">Confidence: <b>${req.confidence ?? "-"}</b> ${lowConf ? '<span class="tag red">low</span>' : ""}${meter(req.confidence)}</div></div>
    <h2>Options</h2>${lowConf ? '<div class="flag amber">Not enough evidence: choose or write an action yourself</div>' : ""}${options || '<div class="muted">No options yet</div>'}
    ${req.status === "review" ? `<form id="custom" class="box"><b>Custom action</b><div class="row"><label>Action</label><input name="title" size="40" required></div>
      <div class="row"><label>Extra cost (Rs)</label><input name="cost" type="number" min="0" value="0" required></div>
      <div class="row"><label>Reason</label><input name="reason" size="40" required></div><button>Use this action</button></form>` : ""}
    ${req.chosen ? `<div class="box">Chosen: <b>${esc(req.chosen.title)}</b> (${rs(req.chosen.est_cost_inr)})</div>` : ""}
    <h2>Approvals</h2>${req.approvals.length ? `<table><tr><th>Role</th><th>Amount</th><th>Due</th><th>Status</th><th></th></tr>${approvalRows(req)}</table>` : '<div class="muted">None</div>'}
    <h2>Status</h2><div>${buttons || '<span class="muted">No actions</span>'} ${req.status === "needs_info" || req.flags.some(f => f.code === "llm_unavailable") ? '<button data-act="retry">Retry analysis</button>' : ""}</div>
    <h2>Messages</h2>${messages}<div><select id="audience">${["requester", "supplier", "store team", "approver"].map(a => `<option>${a}</option>`).join("")}</select>
      <button data-act="draft">Draft message</button></div>
    <h2>Summary</h2><button data-act="summary">Generate summary</button><pre class="text">${esc(req.summary || "")}</pre>
  </div></div>
  <h2>Audit trail</h2><div class="wrap"><table><tr><th>Time</th><th>Actor</th><th>Action</th><th>Detail</th></tr>${auditRows(req)}</table></div>`;
}

async function approvalsView() {
  const rows = await api("/api/approvals?mine=1");
  const body = rows.map(a => `<tr><td><a href="#/requests/${a.request_id}">#${a.request_id} ${esc(a.subject)}</a></td><td>${esc(label(a.role))}</td>
    <td>${rs(a.amount)}</td><td class="${a.overdue ? "overdue" : ""}">${esc(when(a.due_at))}${a.overdue ? " (overdue)" : ""}</td>
    <td><button data-act="decide" data-id="${a.id}" data-approve="1">Approve</button> <button class="danger" data-act="decide" data-id="${a.id}" data-approve="0">Reject</button></td></tr>`).join("");
  return `<h1>Approvals you can decide</h1><div class="wrap"><table><tr><th>Request</th><th>Role</th><th>Amount</th><th>Due</th><th></th></tr>
    ${body || '<tr><td colspan="5" class="muted">Nothing waiting for you</td></tr>'}</table></div>`;
}

function reportView() {
  return `<h1>Management report</h1><button class="primary" data-act="report">Generate report</button><div id="report" class="box" hidden></div>`;
}

async function adminView() {
  const s = await api("/api/admin/state");
  return `<h1>Admin</h1><div class="box"><p>Clock: <b>${esc(when(s.now))}</b> IST (offset ${s.offset_hours} h)</p>
    <button data-act="clock" data-hours="1">+1h</button> <button data-act="clock" data-hours="4">+4h</button> <button data-act="clock" data-hours="">Reset</button></div>
    <div class="box"><p>AI service: <b>${s.llm_down ? "marked unavailable" : "available"}</b></p>
    <button data-act="llm" data-down="${s.llm_down ? 0 : 1}">${s.llm_down ? "Mark available" : "Simulate outage"}</button></div>
    <div class="box"><p>Mail server: <b>${s.outbox_down ? "failing" : "available"}</b></p>
    <button data-act="outbox" data-down="${s.outbox_down ? 0 : 1}">${s.outbox_down ? "Restore" : "Simulate send failure"}</button></div>
    <div class="box"><p>Demo data seeded on ${esc(s.seeded_on)}. Resetting deletes all requests and signs everyone out.</p>
    <button class="danger" data-act="reset">Reset demo data</button></div>
    <p><a href="/api/health" target="_blank">Health check</a></p>`;
}

async function render() {
  clearTimeout(timer);
  const app = document.getElementById("app");
  if (!me) {
    try { me = await fetch("/api/me").then(r => r.ok ? r.json() : null); } catch { me = null; }
  }
  if (!me) { app.innerHTML = loginView(); return; }
  const [, route = "requests", id] = location.hash.split("/");
  let content = "";
  try {
    content = route === "requests" && id ? await detailView(id) : route === "inbox" ? await inboxView()
      : route === "approvals" ? await approvalsView() : route === "report" ? reportView()
      : route === "admin" && me.role === "admin" ? await adminView() : await requestsView();
  } catch { content = '<p class="muted">Could not load this page.</p>'; }
  const pending = await fetch("/api/approvals?mine=1").then(r => r.ok ? r.json() : []).catch(() => []);
  app.innerHTML = shell(content, pending.length);
}

const reqId = () => location.hash.split("/")[2];
const actions = {
  async logout() { await api("/api/logout", {}); me = null; location.hash = ""; },
  async import(d) { const r = await api(`/api/inbox/${d.id}/import`, {}); location.hash = `#/requests/${r.id}`; },
  async edit(d) {
    const value = prompt(`New value for ${label(d.field)} (leave empty for not stated)`);
    if (value === null) return;
    const reason = prompt("Why is this being corrected?") || "";
    await api(`/api/requests/${reqId()}/fields`, { field: d.field, value, reason });
  },
  async choose(d) {
    const req = await api(`/api/requests/${reqId()}`);
    const option = req.options[d.i];
    let reason = "";
    if (!option.recommended || req.flags.some(f => f.code === "low_confidence")) {
      reason = prompt("Why choose this option instead of the recommendation?");
      if (!reason) return;
    }
    await api(`/api/requests/${reqId()}/choose`, { option_index: Number(d.i), reason });
  },
  async decide(d) {
    let reason = "";
    if (d.approve === "0") { reason = prompt("Reason for rejecting"); if (!reason) return; }
    await api(`/api/approvals/${d.id}/decide`, { approve: d.approve === "1", reason });
  },
  async status(d) {
    const note = d.action === "cancel" ? prompt("Why is this being cancelled?") || "" : "";
    await api(`/api/requests/${reqId()}/transition`, { action: d.action, note });
  },
  async retry() { await api(`/api/requests/${reqId()}/retry`, {}); },
  async draft() { await api(`/api/requests/${reqId()}/messages`, { audience: document.getElementById("audience").value }); },
  async summary() { await api(`/api/requests/${reqId()}/summary`, {}); },
  async report() {
    const box = document.getElementById("report");
    box.hidden = false; box.textContent = "Working...";
    try { const r = await api("/api/report", {}); box.innerHTML = `<b>${esc(r.headline)}</b><pre class="text">${esc(r.body)}</pre>`; }
    catch { box.textContent = "Report unavailable."; }
  },
  async clock(d) { await api("/api/admin/clock", { hours: d.hours === "" ? null : Number(d.hours) }); },
  async outbox(d) { await api("/api/admin/outbox", { down: d.down === "1" }); },
  async reset() {
    if (!confirm("Delete all requests and reload the sample data?")) return;
    await api("/api/admin/reset", {}); me = null; location.hash = "";
  },
  async send(d) {
    const r = await api(`/api/messages/${d.id}/send`, {});
    if (r.status === "failed") showError("Message not delivered: the mail server is unavailable. It is marked failed; retry later.");
  },
  async llm(d) { await api("/api/admin/llm", { down: d.down === "1" }); },
};

document.addEventListener("click", async e => {
  const btn = e.target.closest("[data-act]");
  if (!btn) return;
  btn.disabled = true;
  try {
    await actions[btn.dataset.act](btn.dataset);
    if (btn.dataset.act !== "report") await render();
  } catch { /* error already shown in the banner */ }
  btn.disabled = false;
});

document.addEventListener("submit", async e => {
  e.preventDefault();
  const f = Object.fromEntries(new FormData(e.target));
  try {
    if (e.target.id === "login") {
      me = await api("/api/login", { username: f.username, password: f.password });
    } else if (e.target.id === "new-request") {
      const r = await api("/api/requests", f);
      location.hash = `#/requests/${r.id}`;
    } else if (e.target.id === "supplier") {
      await api(`/api/requests/${reqId()}/supplier-update`, { delay_days: Number(f.delay_days), note: f.note });
    } else if (e.target.id === "custom") {
      await api(`/api/requests/${reqId()}/choose`, { custom: { title: f.title, est_cost_inr: Number(f.cost) }, reason: f.reason });
    }
    await render();
  } catch { /* error already shown in the banner */ }
});

window.addEventListener("hashchange", render);
render();
