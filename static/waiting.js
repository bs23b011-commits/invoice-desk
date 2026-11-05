/* "Waiting for you": the agent recommends, a person makes the final call.
   Uses helpers from app.js: $, esc, inr, api, toast, show, openRun, refreshBadge, fmtTime. */
"use strict";

const W = { items: [], sel: null, poll: null, done: {}, busy: false };
const REC = {
  APPROVE: { word: "Approve", tone: "approve", list: "Recommend approve" },
  REVIEW: { word: "Hold", tone: "review", list: "Recommend hold" },
  REJECT: { word: "Reject", tone: "reject", list: "Recommend reject" },
  UNREADABLE: { word: "Get a readable copy", tone: "reject", list: "Can't read this file" },
};
const ICON = {
  ok: `<svg aria-hidden="true" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>`,
  bad: `<svg aria-hidden="true" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>`,
  warn: `<svg aria-hidden="true" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round"><path d="M12 6v7"/><path d="M12 18h.01"/></svg>`,
  agent: `<svg aria-hidden="true" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="8" width="14" height="11" rx="3"/><path d="M12 4v4"/><circle cx="9.5" cy="13.5" r="1"/><circle cx="14.5" cy="13.5" r="1"/></svg>`,
  alert: `<svg aria-hidden="true" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M12 7v6"/><path d="M12 17h.01"/></svg>`,
  clock: `<svg aria-hidden="true" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>`,
};
const initials = (s) => String(s || "?").replace(/\(.*?\)/g, "").split(/[\s/]+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join("").toUpperCase();
const shortInr = (v) => (v == null ? "—" : "₹" + Number(v).toLocaleString("en-IN", { maximumFractionDigits: 0 }));
const dueTxt = (iso) => (iso ? new Date(iso).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "");

async function loadWaiting() {
  stopWaiting();
  await refreshWaiting();
  W.poll = setInterval(() => { if (!document.hidden && !W.busy) refreshWaiting(true); }, 6000);
}
function stopWaiting() { clearInterval(W.poll); W.poll = null; }

async function refreshWaiting(quiet) {
  let d;
  try { d = await api("/api/waiting"); } catch (e) { if (!quiet) toast(e.message); return; }
  const before = JSON.stringify(W.items.map((x) => [x.id, x.recommendation, x.thread.length]));
  W.items = d.items;
  const after = JSON.stringify(W.items.map((x) => [x.id, x.recommendation, x.thread.length]));
  if (quiet && before === after) return;  // nothing new: don't disturb a typed note
  if (!W.items.some((x) => x.id === W.sel) && !W.done[W.sel]) W.sel = W.items[0]?.id || null;
  $("#w-sum").textContent = shortInr(d.total);
  $("#w-count").textContent = d.items.length ? `${d.items.length} invoice${d.items.length > 1 ? "s" : ""} need your decision` : "Nothing needs you right now";
  refreshBadge(d.items.length);
  renderList();
  renderDetail();
}

function renderList() {
  $("#w-items").innerHTML = W.items.map((x) => {
    const r = REC[x.recommendation];
    return `<button type="button" class="w-item ${x.id === W.sel ? "sel" : ""}" data-id="${esc(x.id)}" aria-pressed="${x.id === W.sel}">
      <span class="w-row"><strong>${esc(x.vendor || "Unknown vendor")}</strong><span class="mono">${shortInr(x.total)}</span></span>
      <span class="w-row"><span class="pill ${r.tone}">${r.list}${x.risk.score >= 25 ? ` · risk ${x.risk.score}` : ""}</span>
        <span class="muted small">${x.msme ? "MSME · " : ""}due ${esc(x.due || "—")}</span></span>
    </button>`;
  }).join("") || "";
  document.querySelectorAll(".w-item").forEach((b) => b.addEventListener("click", () => { W.sel = b.dataset.id; renderList(); renderDetail(); }));
}

function gauge(score) {
  const color = score >= 70 ? "var(--reject)" : score >= 25 ? "var(--review)" : "var(--approve)";
  const dash = (Math.min(100, Math.max(0, score)) / 100 * 188.5).toFixed(1);
  return `<svg role="img" aria-label="Fraud risk ${score} out of 100" width="170" height="96" viewBox="0 0 160 90">
    <path d="M 20 80 A 60 60 0 0 1 140 80" fill="none" stroke="var(--rule)" stroke-width="12" stroke-linecap="round"/>
    <path d="M 20 80 A 60 60 0 0 1 140 80" fill="none" stroke="${color}" stroke-width="12" stroke-linecap="round" stroke-dasharray="${dash} 400"/>
    <text x="80" y="76" text-anchor="middle" fill="currentColor" font-family="JetBrains Mono, monospace" font-size="26" font-weight="700">${score}</text></svg>`;
}

function matchBlock(m) {
  if (!m.lines.length) {
    return `<div class="w-card"><div class="eyebrow">3-way match</div>
      <p class="w-nomatch">${m.po_number ? "Purchase order found but no lines could be paired." : "No purchase order on this invoice, so nothing to match yet."}</p></div>`;
  }
  const cell = (label, val, sub, tone) => `<div class="w-cell ${tone ? "is-" + tone : ""}"><div class="eyebrow">${label}</div>
    <div class="mono w-big">${esc(val ?? "—")}</div><div class="muted small">${esc(sub || "")}</div></div>`;
  const rows = m.lines.slice(0, 3).map((l) => {
    const short = l.received != null && l.billed != null && l.received < l.billed;
    return `<div class="w-line">${l.item ? `<div class="muted small w-item-name">${esc(l.item)}</div>` : ""}
      <div class="w-cells">
        ${cell("Ordered", l.ordered, "on the PO", "")}
        ${cell("Received", l.service ? "Service" : l.received, l.service ? "no receipt needed" : short ? `${l.billed - l.received} missing` : "in the warehouse", short ? "bad" : "")}
        ${cell("Billed", l.billed, l.price_ok ? "price as PO" : "price above PO", l.price_ok && (!l.ordered || l.billed <= l.ordered) ? "" : "bad")}
      </div></div>`;
  }).join("");
  return `<div class="w-card"><div class="eyebrow">3-way match · ${esc(m.po_number || "")}${m.how && m.how !== "quoted on invoice" ? ` (${esc(m.how)})` : ""}</div>${rows}
    ${m.lines.length > 3 ? `<p class="muted small">+ ${m.lines.length - 3} more lines</p>` : ""}</div>`;
}

function renderDetail() {
  const el = $("#w-detail"), side = $("#w-side");
  const done = W.done[W.sel];
  if (done) return renderDone(done);
  const x = W.items.find((i) => i.id === W.sel);
  if (!x) {
    el.innerHTML = `<div class="w-empty"><div class="w-empty-mark">${ICON.ok}</div><h1>Nothing is waiting for you</h1>
      <p class="muted">When the agent finishes an invoice, it lands here with its recommendation. Nothing is posted or paid until you approve.</p>
      <button class="btn" type="button" id="w-go-process">Process an invoice</button></div>`;
    side.innerHTML = "";
    $("#w-go-process").addEventListener("click", () => show("process"));
    return;
  }
  if (x.recommendation === "UNREADABLE") return renderUnreadable(x);
  const r = REC[x.recommendation];
  const checks = x.checks.map((c) => {
    const t = c.ok ? "ok" : c.status === "fail" ? "bad" : "warn";
    return `<div class="w-check is-${t}"><span class="w-dot">${ICON[t]}</span><span><strong>${esc(c.label)}</strong><span class="muted small">${esc(c.detail)}</span></span></div>`;
  }).join("");
  el.innerHTML = `
    <div class="w-card w-head">
      <div class="w-head-l">
        <div class="mono muted small">${esc(x.invoice_number || "no number")} · ${esc(x.channel)}</div>
        <h1>${esc(x.vendor || "Unknown vendor")}</h1>
        <div class="w-bars" aria-label="All checks run">${Array.from({ length: 11 }, () => `<span></span>`).join("")}
          <span class="muted small">11 stages run${x.duration ? ` · ${Number(x.duration).toFixed(1)} s` : ""}</span></div>
      </div>
      <div class="w-head-r"><div class="mono w-amount">${shortInr(x.total)}</div><div class="muted small">due ${esc(x.due || "—")}${x.msme ? " · MSME" : ""}</div></div>
    </div>
    <div class="w-agent ${r.tone}">
      <div class="w-agent-icon">${ICON.agent}</div>
      <div><div class="eyebrow tone">Agent recommends · ${r.word}</div><p>${esc(x.note)}</p></div>
    </div>
    <div class="w-grid2">
      ${matchBlock(x.match)}
      <div class="w-card w-risk"><div class="eyebrow">Fraud risk</div>${gauge(x.risk.score)}
        <div class="small w-risk-word ${x.risk.score >= 70 ? "reject" : x.risk.score >= 25 ? "review" : "approve"}">${x.risk.score >= 70 ? "Block payment" : x.risk.score >= 25 ? "Phone check needed" : "Low"}</div>
        ${x.risk.signals.length ? `<ul class="w-signals">${x.risk.signals.map((s) => `<li>${esc(s)}</li>`).join("")}</ul>` : ""}</div>
    </div>
    <div class="w-card"><div class="eyebrow">Why</div><div class="w-checks">${checks}</div></div>
    <div class="w-card w-act">
      <div class="small"><span class="muted">If you approve →</span> ${esc(x.impact)}</div>
      <label class="w-note">${x.recommendation === "REJECT" ? "Why approve anyway? (required)" : "Your note (optional, saved in the audit trail)"}
        <input type="text" id="w-note" placeholder="${x.recommendation === "REJECT" ? "At least 10 characters" : "e.g. checked with Ravi in Stores"}"></label>
      ${x.needs_phone_check ? `<label class="w-verify"><input type="checkbox" id="w-verified"> I called the vendor on the number already in the vendor master (not the one on the invoice)</label>` : ""}
      <label class="w-note w-who">Deciding as <input type="text" id="w-who" value="${esc(localGet("who") || "AP manager")}" aria-label="Your name"></label>
      <div class="w-btns">
        <button type="button" class="w-btn approve" data-act="approve">${x.recommendation === "APPROVE" ? "Approve &amp; schedule payment" : "Approve anyway"}</button>
        <button type="button" class="w-btn reject" data-act="reject">${x.recommendation === "REJECT" ? "Confirm reject" : "Reject"}</button>
        <button type="button" class="w-btn ghost" id="w-open">Open full details</button>
      </div>
      <div class="muted small">Nothing is posted or paid until you decide.</div>
    </div>
    ${x.onboard ? onboardHtml(x.onboard) : ""}`;
  side.innerHTML = sideHtml(x);
  el.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", () => decide(x, b.dataset.act)));
  $("#w-open").addEventListener("click", () => { state.open = new Set(["decision"]); state.lastStatus = {}; show("process"); openRun(x.id); });
  bindSim(x);
  if (x.onboard) bindOnboard(x);
}

function renderUnreadable(x) {
  $("#w-detail").innerHTML = `
    <div class="w-card w-head"><div class="w-head-l"><div class="mono muted small">${esc(x.invoice_number || "")} · ${esc(x.channel)}</div>
      <h1>${esc(x.vendor || "Unknown sender")}</h1></div></div>
    <div class="w-agent reject"><div class="w-agent-icon">${ICON.alert}</div>
      <div><div class="eyebrow tone">Can't read this file</div><p>${esc(x.note)}</p></div></div>
    <div class="w-card w-act">
      <div class="small">${esc(x.impact)}</div>
      <label class="w-note">Note (optional)<input type="text" id="w-note" placeholder="e.g. asked the vendor for a new copy"></label>
      <label class="w-note w-who">Deciding as <input type="text" id="w-who" value="${esc(localGet("who") || "AP manager")}" aria-label="Your name"></label>
      <div class="w-btns"><button type="button" class="w-btn reject" data-act="reject">Dismiss: asked for a new copy</button></div>
      <div class="muted small">It stays in Records, so nothing that arrived is ever lost.</div>
    </div>`;
  $("#w-side").innerHTML = sideHtml(x);
  $("#w-detail").querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", () => decide(x, b.dataset.act)));
}

function onboardHtml(o) {
  const bankName = (o.bank || "").split(" ****")[0];
  return `<div class="w-card w-onboard"><div class="eyebrow">Onboard this vendor</div>
    <p class="small muted">The agent held this invoice because the vendor isn't in the vendor master. If it's a real supplier,
      add it here and the agent re-checks the invoice. The bank account must come from the vendor's KYC or a phone call,
      not from the invoice.</p>
    <div class="w-onb-grid">
      <label class="w-note">Legal name<input id="onb-name" value="${esc(o.name)}"></label>
      <label class="w-note">GSTIN<input id="onb-gstin" value="${esc(o.gstin)}" class="mono"></label>
      <label class="w-note">Bank<input id="onb-bank" value="${esc(bankName)}" placeholder="e.g. ICICI Bank"></label>
      <label class="w-note">Account number (confirmed)<input id="onb-acct" inputmode="numeric" placeholder="${o.bank ? "invoice says " + esc(o.bank) : "full account number"}"></label>
      <label class="w-note">Email<input id="onb-email" type="email" placeholder="accounts@vendor.com"></label>
      <label class="w-note">Payment terms (days)<input id="onb-terms" type="number" value="30" min="0" max="180"></label>
    </div>
    <label class="w-verify"><input type="checkbox" id="onb-msme"> MSME vendor (Udyam registered: must be paid within 45 days)</label>
    <label class="w-verify"><input type="checkbox" id="onb-verified"> I verified this vendor's identity and bank account (KYC documents or a call on a known number)</label>
    <div class="w-btns"><button type="button" class="w-btn ghost" id="onb-go">Onboard vendor &amp; re-check invoice</button></div>
  </div>`;
}

function bindOnboard(x) {
  $("#onb-go").addEventListener("click", async () => {
    const fd = new FormData();
    fd.append("name", $("#onb-name").value); fd.append("gstin", $("#onb-gstin").value);
    fd.append("bank_name", $("#onb-bank").value); fd.append("account_number", $("#onb-acct").value);
    fd.append("email", $("#onb-email").value); fd.append("terms_days", $("#onb-terms").value || "30");
    fd.append("msme", $("#onb-msme").checked ? "1" : ""); fd.append("verified", $("#onb-verified").checked ? "1" : "");
    fd.append("who", ($("#w-who")?.value || "Vendor master team").trim());
    W.busy = true;
    $("#onb-go").disabled = true;
    try {
      const j = await api(`/api/runs/${x.id}/onboard`, { method: "POST", body: fd });
      toast(`${j.vendor.name} added to the vendor master. Re-checking the invoice…`);
      for (let i = 0; i < 60; i++) {
        const run = await api(`/api/runs/${x.id}`);
        if (run.state !== "running") break;
        await new Promise((ok) => setTimeout(ok, 300));
      }
    } catch (e) { toast(e.message); $("#onb-go").disabled = false; }
    W.busy = false;
    await refreshWaiting();
  });
}

// the email as the person reads it, without the greeting, the [Ref] reminder and the signature
const tidy = (b) => String(b || "").replace(/^\s*Dear[^\n]*\n+/, "").split(/\n\s*(Please keep \[Ref|Regards,)/)[0].trim();

function sideHtml(x) {
  const o = x.owner;
  const bubbles = x.thread.map((m) => `<div class="w-msg ${m.direction}">
      <div class="w-msg-h">${esc(m.direction === "out" ? m.who : m.who + " → Agent")} · ${m.at ? esc(fmtTime(m.at)) : ""}</div>
      <div class="w-msg-b">${esc(tidy(m.body))}</div>
      ${m.understood ? `<div class="w-msg-u">Understood as <b>${esc(m.understood.replace(/_/g, " "))}</b>${m.outcome ? `. ${esc(m.outcome)}` : ""}</div>` : ""}
    </div>`).join("");
  const waitingFor = x.recommendation !== "APPROVE" && o.due
    ? `<div class="w-wait">${ICON.clock}<span>Waiting for ${esc(o.name)} · answer by ${esc(dueTxt(o.due))}, then it goes to ${esc(o.escalate_to)}</span></div>` : "";
  const sim = x.reply_examples.length ? `<div class="w-card w-sim"><div class="eyebrow">Simulate a reply</div>
      <p class="muted small">In production replies arrive by email through n8n. Try one:</p>
      <div class="w-sim-btns">${x.reply_examples.map((e, i) => `<button type="button" class="w-chip" data-ex="${i}">${esc(e.label)}</button>`).join("")}</div></div>` : "";
  return `
    <div class="w-card"><div class="eyebrow">${x.recommendation === "APPROVE" ? "Final OK from" : "Who can resolve it"}</div>
      <div class="w-owner"><div class="w-avatar">${esc(initials(o.name))}</div>
        <div><strong>${esc(o.name)}</strong><div class="muted small">${esc(o.sub)}${o.more ? ` (+${o.more} more)` : ""}</div></div></div>
      ${o.question ? `<p class="w-q">“${esc(o.question)}”</p>` : ""}</div>
    <div class="w-card"><div class="eyebrow">Conversation</div>
      <div class="w-thread">${bubbles || `<p class="muted small">${x.recommendation === "REJECT" ? "Nobody was contacted: suspected fraud is never answered." : "No messages needed."}</p>`}${waitingFor}</div></div>
    ${sim}`;
}

function bindSim(x) {
  document.querySelectorAll(".w-sim [data-ex]").forEach((b) => b.addEventListener("click", async () => {
    const e = x.reply_examples[+b.dataset.ex];
    const fd = new FormData(); fd.append("party", e.party); fd.append("body", e.text);
    W.busy = true;
    document.querySelectorAll(".w-sim button").forEach((y) => (y.disabled = true));
    try {
      const j = await api(`/api/runs/${x.id}/reply`, { method: "POST", body: fd });
      toast(j.outcome?.text || "Reply received. Re-checking.");
      for (let i = 0; i < 60; i++) {  // the agent re-runs every check; wait for it
        const run = await api(`/api/runs/${x.id}`);
        if (run.state !== "running") break;
        await new Promise((ok) => setTimeout(ok, 300));
      }
    } catch (err) { toast(err.message); }
    W.busy = false;
    await refreshWaiting();
  }));
}

async function decide(x, act) {
  if (W.busy) return;
  const note = $("#w-note")?.value || "";
  const who = ($("#w-who")?.value || "AP manager").trim();
  localSet("who", who);
  const fd = new FormData();
  fd.append("action", act); fd.append("note", note); fd.append("who", who);
  fd.append("verified", $("#w-verified")?.checked ? "1" : "");
  W.busy = true;
  document.querySelectorAll(".w-btn").forEach((b) => (b.disabled = true));
  try {
    const run = await api(`/api/runs/${x.id}/resolve`, { method: "POST", body: fd });
    W.done[x.id] = { x, run, act, step: 1 };
    renderDetail();
    if (act === "approve") {  // show the money moving, one step at a time
      setTimeout(() => { if (W.done[x.id]) { W.done[x.id].step = 2; if (W.sel === x.id) renderDone(W.done[x.id]); } }, 650);
      setTimeout(() => { if (W.done[x.id]) { W.done[x.id].step = 3; if (W.sel === x.id) renderDone(W.done[x.id]); } }, 1300);
    }
    W.items = W.items.filter((i) => i.id !== x.id);
    refreshBadge(W.items.length);
    renderList();
    $("#w-sum").textContent = shortInr(W.items.reduce((a, i) => a + (i.total || 0), 0));
    $("#w-count").textContent = W.items.length ? `${W.items.length} invoice${W.items.length > 1 ? "s" : ""} need your decision` : "Nothing needs you right now";
  } catch (e) {
    toast(e.message);
    document.querySelectorAll(".w-btn").forEach((b) => (b.disabled = false));
  }
  W.busy = false;
}

function renderDone(d) {
  const { x, run, act, step } = d;
  const res = run.result || {};
  const pay = res.payment || {};
  const on = (n) => (step >= n ? "on" : "");
  const steps = act === "approve" ? `
      <li class="${on(1)}">Approved by ${esc(run.resolution.by)}${run.resolution.note ? `: “${esc(run.resolution.note)}”` : ""}</li>
      <li class="${on(2)}">${step >= 2 ? `Posted to the ERP as <b class="mono">${esc(res.erp_doc || "—")}</b>, read back and checked` : "Posting to the ERP…"}</li>
      <li class="${on(3)}">${step >= 3 ? `${shortInr(pay.amount)} scheduled for ${esc(x.due || "its due date")} to ${esc(pay.bank || "the account on file")}` : "Scheduling payment…"}</li>`
    : `<li class="on">Rejected by ${esc(run.resolution.by)}${run.resolution.note ? `: “${esc(run.resolution.note)}”` : ""}</li>
       <li class="on">Removed from the payment queue. The PO balance is released.</li>`;
  $("#w-detail").innerHTML = `
    <div class="w-card w-done ${act === "approve" ? "approve" : "reject"}" role="status">
      <div class="eyebrow tone">${act === "approve" ? "Approved" : "Rejected"}</div>
      <h1>${esc(x.vendor)} · <span class="mono">${shortInr(x.total)}</span></h1>
      <ol class="w-steps">${steps}</ol>
      <div class="w-btns">
        ${W.items.length ? `<button type="button" class="w-btn approve" id="w-next">Next invoice →</button>` : `<span class="muted">That was the last one. Nothing else is waiting for you.</span>`}
        ${act === "approve" ? `<button type="button" class="w-btn ghost" id="w-pay">See it in Payments</button>` : ""}
      </div>
    </div>`;
  $("#w-next")?.addEventListener("click", () => { delete W.done[x.id]; W.sel = W.items[0]?.id || null; renderList(); renderDetail(); });
  $("#w-pay")?.addEventListener("click", () => show("payments"));
}

function move(delta) {
  if (!W.items.length) return;
  const i = W.items.findIndex((x) => x.id === W.sel);
  if (W.done[W.sel]) delete W.done[W.sel];
  W.sel = W.items[(Math.max(i, 0) + delta + W.items.length) % W.items.length].id;
  renderList(); renderDetail();
  document.querySelector(".w-item.sel")?.scrollIntoView({ block: "nearest" });
}
document.addEventListener("keydown", (e) => {
  if ($("#view-waiting").hidden || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.target.closest("input, textarea, select")) return;
  const k = e.key.toLowerCase();
  if (k === "j" || e.key === "ArrowDown") { e.preventDefault(); move(1); }
  else if (k === "k" || e.key === "ArrowUp") { e.preventDefault(); move(-1); }
  else if (k === "a" || k === "r") {
    const x = W.items.find((i) => i.id === W.sel);
    if (x && !W.done[x.id]) decide(x, k === "a" ? "approve" : "reject");
  }
});

/* light / dark, remembered per browser */
function localGet(k) { try { return localStorage.getItem("invoice-desk:" + k); } catch (_) { return null; } }
function localSet(k, v) { try { localStorage.setItem("invoice-desk:" + k, v); } catch (_) {} }
(function theme() {
  const saved = localGet("theme");
  if (saved === "light" || saved === "dark") document.documentElement.dataset.theme = saved;
  $("#theme").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localSet("theme", next);
  });
})();
