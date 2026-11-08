/* "Waiting for you": the agent recommends, a person makes the final call.
   Uses helpers from app.js: $, esc, inr, api, toast, show, openRun, refreshBadge, fmtTime. */
"use strict";

const W = { items: [], sel: null, poll: null, done: {}, busy: false, mode: "list", filter: "all", approver: "AP manager" };
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

/* ---------------------------------------------------------------- plain-language status for each invoice */
const firstSentence = (t) => String(t || "").split(/(?<=[.!?])\s/)[0];
const vname = (x) => String(x.vendor || "Unknown vendor").replace(/\s*\(imitating[^)]*\)/i, " (look-alike)");
const shortName = (n) => String(n || "").replace(/\s*\(.*?\)/, "").replace(/ \/ receiving$/, "");
function waitingOn(x) {
  return x.recommendation === "REVIEW" && !x.needs_phone_check && x.owner && x.owner.name !== W.approver
    && x.thread.some((m) => m.direction === "out");
}
function cat(x) {
  if (x.recommendation === "REJECT" || x.recommendation === "UNREADABLE") return "stop";
  return waitingOn(x) ? "waiting" : "you";
}
function statusLine(x) {
  if (x.recommendation === "UNREADABLE") return "Can't read this file. Ask the sender for a readable copy";
  if (x.recommendation === "REJECT") return "Don't pay. " + firstSentence(x.note);
  if (x.recommendation === "APPROVE") return `Ready: every check passed${x.due ? ` · pays ${x.due}` : ""}`;
  if (x.needs_phone_check) return "New bank account on the invoice. Call them on the number on file first";
  if (waitingOn(x)) return `Waiting for ${shortName(x.owner.name)}: ${firstSentence(x.owner.question || x.note)}`;
  return firstSentence(x.note);
}
const PILL = { you: ["Needs you", "review"], waiting: ["Waiting", "info"], stop: ["Don't pay", "reject"] };
function pill(x) {
  if (x.recommendation === "APPROVE") return ["Ready", "approve"];
  if (x.recommendation === "UNREADABLE") return ["Can't read", "reject"];
  return PILL[cat(x)];
}
function steps(x) {
  const ok = "approve", warn = "review", bad = "reject", now = "now", todo = "todo";
  const got = { label: "Received", tone: ok, sub: `${x.channel}${x.created ? ", " + dTimeShort(x.created) : ""}` };
  if (x.recommendation === "UNREADABLE") {
    return [got, { label: "Can't read", tone: bad, sub: "file damaged or locked" }, { label: "You decide", tone: now },
            { label: "Checked", tone: todo }, { label: "Paid", tone: todo }];
  }
  const checked = { label: "Checked", tone: ok, sub: `${x.stages_done || 11} checks` };
  let third;
  if (x.recommendation === "APPROVE") third = { label: x.thread.some((m) => m.direction === "in") ? "Questions answered" : "All checks passed", tone: ok };
  else if (x.recommendation === "REJECT") third = { label: "Problem found", tone: bad };
  else if (waitingOn(x)) third = { label: `Waiting for ${shortName(x.owner.name)}`, tone: warn, sub: x.owner.due ? "reply by " + dueTxt(x.owner.due) : "" };
  else third = { label: "Needs a look", tone: warn };
  return [got, checked, third, { label: "You decide", tone: waitingOn(x) ? todo : now }, { label: "Paid", tone: todo, sub: x.due || "" }];
}
const dTimeShort = (iso) => { try { return new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short" }); } catch (_) { return ""; } };
function tracker(x) {
  return `<ol class="c-track" aria-label="Where this invoice is">${steps(x).map((s) =>
    `<li class="t-${s.tone}"><span class="c-bar"></span><span class="c-step">${esc(s.label)}</span>${s.sub ? `<span class="c-sub">${esc(s.sub)}</span>` : ""}</li>`).join("")}</ol>`;
}
const AGENT_SAYS = { APPROVE: ["approve.", "approve"], REVIEW: ["wait.", "review"], REJECT: ["don't pay.", "reject"], UNREADABLE: ["get a readable copy.", "reject"] };
function agentSays(x) {
  const [w, tone] = AGENT_SAYS[x.recommendation];
  return `<div class="c-says t-${tone}"><b>Agent says: ${w}</b> ${esc(x.note)}</div>`;
}

/* ---------------------------------------------------------------- data + the list */
async function refreshWaiting(quiet) {
  let d;
  try { d = await api("/api/waiting"); } catch (e) { if (!quiet) toast(e.message); return; }
  const sig = (xs) => JSON.stringify(xs.map((x) => [x.id, x.recommendation, x.thread.length, x.comments]));
  const before = sig(W.items);
  W.items = d.items;
  W.approver = d.approver;
  if (quiet && before === sig(W.items)) return;  // nothing new: don't disturb a typed note
  if (W.mode === "detail" && !W.items.some((x) => x.id === W.sel) && !W.done[W.sel]) W.mode = "list";
  refreshBadge(d.items.length);
  renderList();
  if (W.mode === "detail") renderDetail();
}

function renderList() {
  const counts = { all: W.items.length, you: 0, waiting: 0, stop: 0 };
  W.items.forEach((x) => counts[cat(x)]++);
  const f = W.filter || "all";
  const rank = (x) => (x.recommendation === "APPROVE" ? 0 : cat(x) === "you" ? 1 : cat(x) === "stop" ? 2 : 3);
  const items = W.items.filter((x) => f === "all" || cat(x) === f).sort((a, b) => rank(a) - rank(b));
  const total = W.items.reduce((a, i) => a + (i.total || 0), 0);
  $("#w-count").textContent = W.items.length
    ? `${W.items.length} invoice${W.items.length > 1 ? "s" : ""} waiting for a decision · ${shortInr(total)}`
    : "Nothing needs you right now";
  $("#w-filters").innerHTML = [["all", "All"], ["you", "Need you"], ["waiting", "Waiting on others"], ["stop", "Don't pay"]]
    .map(([k, l]) => `<button type="button" class="c-chip ${f === k ? "on" : ""}" data-f="${k}" aria-pressed="${f === k}">${l} · ${counts[k]}</button>`).join("");
  document.querySelectorAll("#w-filters .c-chip").forEach((b) => b.addEventListener("click", () => { W.filter = b.dataset.f; renderList(); }));
  const [top, ...rest] = items;
  $("#w-featured").innerHTML = top ? featured(top) : (W.items.length ? `<p class="muted c-none">Nothing in this group.</p>` : emptyState());
  $("#w-items").innerHTML = rest.length ? `<div class="c-rows">${rest.map(row).join("")}</div>` : "";
  document.querySelectorAll("[data-open]").forEach((b) => b.addEventListener("click", () => openDetail(b.dataset.open)));
  document.querySelectorAll("[data-quick]").forEach((b) => b.addEventListener("click", () => {
    const x = W.items.find((i) => i.id === b.dataset.quick);
    openDetail(x.id);
    decide(x, "approve");
  }));
  $("#w-go-process")?.addEventListener("click", () => show("process"));
}

function featured(x) {
  const quick = x.recommendation === "APPROVE" && !x.needs_phone_check;
  return `<section class="c-card c-feature">
    <div class="c-row-top">
      <div><span class="c-vendor">${esc(vname(x))}</span> <span class="muted">· ${esc(x.invoice_number || "no number")}</span>
        ${x.batch ? `<span class="c-batch ${x.batch.mixed ? "mixed" : ""}">${x.batch.index} of ${x.batch.count} in ${esc(x.batch.source)}</span>` : ""}</div>
      <span class="c-amount">${shortInr(x.total)}</span>
    </div>
    ${tracker(x)}
    ${agentSays(x)}
    <div class="c-btns">
      ${quick ? `<button type="button" class="c-btn approve" data-quick="${esc(x.id)}">Approve payment</button>` : ""}
      <button type="button" class="c-btn ${quick ? "ghost" : "primary"}" data-open="${esc(x.id)}">${quick ? "Details" : "Open and decide"}</button>
    </div>
  </section>`;
}

function row(x) {
  const [label, tone] = pill(x);
  return `<button type="button" class="c-listrow" data-open="${esc(x.id)}">
    <span class="c-who"><b>${esc(vname(x))}</b><span class="muted">${shortInr(x.total)}${x.batch ? ` · ${x.batch.index} of ${x.batch.count} in ${esc(x.batch.source)}` : ""}</span></span>
    <span class="c-what"><span class="c-dot t-${tone}" aria-hidden="true"></span>${esc(statusLine(x))}</span>
    <span class="c-pill t-${tone}">${label}</span>
  </button>`;
}

function emptyState() {
  return `<div class="c-card c-empty"><div class="w-empty-mark">${ICON.ok}</div><h2>Nothing is waiting for you</h2>
    <p class="muted">When the agent finishes an invoice, it lands here with its advice. Nothing is posted or paid until you approve.</p>
    <button class="c-btn primary" type="button" id="w-go-process">Upload an invoice</button></div>`;
}

function openDetail(id) {
  W.sel = id; W.mode = "detail";
  $("#w-listview").hidden = true; $("#w-detailview").hidden = false;
  renderDetail();
  window.scrollTo({ top: 0 });
}
function backToList() {
  if (W.done[W.sel]) delete W.done[W.sel];
  W.mode = "list";
  $("#w-detailview").hidden = true; $("#w-listview").hidden = false;
  renderList();
}

/* ---------------------------------------------------------------- one invoice */
function matchBlock(m) {
  if (!m.lines.length) {
    return `<section class="c-card"><h2 class="c-h">Did we get what we paid for?</h2>
      <p class="muted">${m.po_number ? "The purchase order was found, but its lines couldn't be paired with the invoice." : "There's no purchase order on this invoice, so there's nothing to compare yet."}</p></section>`;
  }
  const box = (label, val, sub, bad, good) => `<div class="c-box ${bad ? "bad" : ""}"><div class="c-box-l">${label}</div>
    <div class="c-box-n">${esc(val ?? "—")}</div><div class="c-box-s ${good ? "good" : ""}">${esc(sub || "")}</div></div>`;
  const rows = m.lines.slice(0, 3).map((l) => {
    const short = l.received != null && l.billed != null && l.received < l.billed;
    const over = l.ordered != null && l.billed > l.ordered;
    return `${m.lines.length > 1 && l.item ? `<div class="c-item">${esc(l.item)}</div>` : ""}<div class="c-boxes">
      ${box("We ordered", l.ordered, "on the PO", false)}
      ${box("We received", l.service ? "Service" : l.received, l.service ? "no delivery to count" : short ? `${l.billed - l.received} still missing` : "in the warehouse", short)}
      ${box("They billed", l.billed, !l.price_ok ? "price above the PO" : over ? "more than ordered" : "price matches the PO", !l.price_ok || over, l.price_ok && !over)}
    </div>`;
  }).join("");
  return `<section class="c-card"><h2 class="c-h">Did we get what we paid for?${m.po_number ? ` <span class="muted c-hint">${esc(m.po_number)}</span>` : ""}</h2>${rows}
    ${m.lines.length > 3 ? `<p class="muted small">+ ${m.lines.length - 3} more lines</p>` : ""}</section>`;
}

function checkChips(x) {
  return `<div class="c-checks">${x.checks.map((c) => {
    const t = c.ok ? "approve" : c.status === "fail" ? "reject" : "review";
    return `<span class="c-check t-${t}" title="${esc(c.detail)}">${c.ok ? ICON.ok : t === "reject" ? ICON.bad : ICON.warn} ${esc(c.label)}</span>`;
  }).join("")}</div>
  ${x.risk.signals.length ? `<div class="c-signals"><b>Fraud signals:</b> ${x.risk.signals.map(esc).join(" · ")}</div>` : ""}`;
}

function renderDetail() {
  const el = $("#w-detail"), side = $("#w-side");
  const done = W.done[W.sel];
  if (done) { side.innerHTML = ""; return renderDone(done); }
  const x = W.items.find((i) => i.id === W.sel);
  if (!x) return backToList();
  if (x.recommendation === "UNREADABLE") return renderUnreadable(x);
  const i = W.items.indexOf(x);
  el.innerHTML = `
    <section class="c-card c-feature">
      <div class="c-row-top">
        <div><div class="c-vendor big">${esc(vname(x))}</div>
          <div class="muted">${esc(x.invoice_number || "no number")}${x.match.po_number ? " · " + esc(x.match.po_number) : ""} · arrived by ${esc(x.channel)}${x.created ? ", " + dTimeShort(x.created) : ""}${x.batch ? ` · invoice ${x.batch.index} of ${x.batch.count} in ${esc(x.batch.source)}` : ""}</div></div>
        <div class="c-amount-wrap"><div class="c-amount big">${shortInr(x.total)}</div><div class="muted small">due ${esc(x.due || "—")}${x.msme ? " · MSME" : ""}</div></div>
      </div>
      ${tracker(x)}
      ${agentSays(x)}
    </section>
    ${matchBlock(x.match)}
    <section class="c-card"><h2 class="c-h">The checks</h2>${checkChips(x)}</section>
    ${x.onboard ? onboardHtml(x.onboard) : ""}
    <section class="c-card c-decide">
      <div class="c-impact"><span class="muted">If you approve:</span> ${esc(x.impact)}</div>
      <label class="w-note">${x.recommendation === "REJECT" ? "Why approve anyway? (required)" : "Your note (optional, saved in the audit trail)"}
        <input type="text" id="w-note" placeholder="${x.recommendation === "REJECT" ? "At least 10 characters" : "e.g. checked with Ravi in Stores"}"></label>
      ${x.needs_phone_check ? `<label class="w-verify"><input type="checkbox" id="w-verified"> I called the vendor on the number already in the vendor master (not the one on the invoice)</label>` : ""}
      <div class="c-btns">
        <button type="button" class="c-btn ${x.recommendation === "APPROVE" ? "approve" : "ghost-approve"} w-btn" data-act="approve">${x.recommendation === "APPROVE" ? "Approve payment" : "Approve anyway"}</button>
        <button type="button" class="c-btn ghost-reject w-btn" data-act="reject">${x.recommendation === "REJECT" ? "Confirm: don't pay" : "Reject"}</button>
        <button type="button" class="c-btn ghost" id="w-open">See every check</button>
        <label class="w-who">Deciding as <input type="text" id="w-who" value="${esc(localGet("who") || "AP manager")}" aria-label="Your name"></label>
      </div>
      <div class="muted small">Nothing is posted or paid until you decide. Keys: <kbd>A</kbd> approve · <kbd>R</kbd> reject · <kbd>J</kbd>/<kbd>K</kbd> next/previous (${i + 1} of ${W.items.length})</div>
    </section>`;
  side.innerHTML = sideHtml(x);
  el.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", () => decide(x, b.dataset.act)));
  $("#w-open").addEventListener("click", () => { state.open = new Set(["decision"]); state.lastStatus = {}; show("process"); openRun(x.id); });
  bindSim(x);
  if (x.onboard) bindOnboard(x);
  if (typeof loadDiscussion === "function") loadDiscussion(x.id);
}

function renderUnreadable(x) {
  $("#w-detail").innerHTML = `
    <section class="c-card c-feature">
      <div class="c-row-top"><div><div class="c-vendor big">${esc(x.vendor || "Unknown sender")}</div>
        <div class="muted">${esc(x.invoice_number || "")} · arrived by ${esc(x.channel)}</div></div></div>
      ${tracker(x)}
      ${agentSays(x)}
    </section>
    <section class="c-card c-decide">
      <div class="c-impact">${esc(x.impact)}</div>
      <label class="w-note">Note (optional)<input type="text" id="w-note" placeholder="e.g. asked the vendor for a new copy"></label>
      <div class="c-btns"><button type="button" class="c-btn ghost-reject w-btn" data-act="reject">Dismiss: asked for a new copy</button>
        <label class="w-who">Deciding as <input type="text" id="w-who" value="${esc(localGet("who") || "AP manager")}" aria-label="Your name"></label></div>
      <div class="muted small">It stays in Records, so nothing that arrived is ever lost.</div>
    </section>`;
  $("#w-side").innerHTML = sideHtml(x);
  if (typeof loadDiscussion === "function") loadDiscussion(x.id);
  $("#w-detail").querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", () => decide(x, b.dataset.act)));
}

function onboardHtml(o) {
  const bankName = (o.bank || "").split(" ****")[0];
  return `<section class="c-card w-onboard"><h2 class="c-h">Onboard this vendor</h2>
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
    <div class="c-btns"><button type="button" class="c-btn primary" id="onb-go">Onboard vendor &amp; re-check invoice</button></div>
  </section>`;
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
  const waitingFor = x.recommendation !== "APPROVE" && o.due
    ? `<div class="w-wait">${ICON.clock}<span>Waiting for ${esc(o.name)} · reply by ${esc(dueTxt(o.due))}, then it goes to ${esc(o.escalate_to)}</span></div>` : "";
  const sim = x.reply_examples.length ? `<section class="c-card w-sim"><h2 class="c-h">Try a reply (demo)</h2>
      <p class="muted small">In real use, replies arrive by email through n8n. Pretend one came in:</p>
      <div class="w-sim-btns">${x.reply_examples.map((e, i) => `<button type="button" class="w-chip" data-ex="${i}">${esc(e.label)}</button>`).join("")}</div></section>` : "";
  return `
    <section class="c-card"><h2 class="c-h">${x.recommendation === "APPROVE" ? "Final OK from" : "Who can sort it out"}</h2>
      <div class="w-owner"><div class="w-avatar">${esc(initials(o.name))}</div>
        <div><strong>${esc(o.name)}</strong><div class="muted small">${esc(o.sub)}${o.more ? ` (+${o.more} more)` : ""}</div></div></div>
      ${o.question ? `<p class="w-q">“${esc(o.question)}”</p>` : ""}</section>
    <section class="c-card w-disc" id="w-disc" data-run="${esc(x.id)}"><h2 class="c-h">Discussion</h2>
      ${waitingFor}<div class="disc-body"><p class="muted small">Loading…</p></div></section>
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
    <div class="c-card w-done ${act === "approve" ? "approve" : "reject"}" role="status">
      <div class="eyebrow tone">${act === "approve" ? "Approved" : "Rejected"}</div>
      <h1>${esc(x.vendor)} · <span class="mono">${shortInr(x.total)}</span></h1>
      <ol class="w-steps">${steps}</ol>
      <div class="w-btns">
        ${W.items.length ? `<button type="button" class="c-btn primary" id="w-next">Next invoice →</button>` : `<span class="muted">That was the last one. Nothing else is waiting for you.</span>`}
        <button type="button" class="c-btn ghost" id="w-all">All invoices</button>
        ${act === "approve" ? `<button type="button" class="c-btn ghost" id="w-pay">See it in Payments</button>` : ""}
      </div>
    </div>`;
  $("#w-next")?.addEventListener("click", () => { delete W.done[x.id]; openDetail(W.items[0].id); });
  $("#w-all")?.addEventListener("click", backToList);
  $("#w-pay")?.addEventListener("click", () => show("payments"));
}

function move(delta) {
  if (!W.items.length) return;
  const i = W.items.findIndex((x) => x.id === W.sel);
  if (W.done[W.sel]) delete W.done[W.sel];
  openDetail(W.items[(Math.max(i, 0) + delta + W.items.length) % W.items.length].id);
}
document.addEventListener("keydown", (e) => {
  if ($("#view-waiting").hidden || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.target.closest("input, textarea, select")) return;
  const k = e.key.toLowerCase();
  if (e.key === "Escape" && W.mode === "detail") { backToList(); return; }
  if (k === "j") { e.preventDefault(); move(1); }
  else if (k === "k") { e.preventDefault(); move(-1); }
  else if ((k === "a" || k === "r") && W.mode === "detail") {
    const x = W.items.find((i) => i.id === W.sel);
    if (x && !W.done[x.id]) decide(x, k === "a" ? "approve" : "reject");
  }
});
document.addEventListener("click", (e) => { if (e.target.closest("#w-back")) backToList(); });

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
