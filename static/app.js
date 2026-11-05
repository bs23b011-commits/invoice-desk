"use strict";
const $ = (s, el = document) => el.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const inrFmt = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", minimumFractionDigits: 2 });
const inr = (v) => (v === null || v === undefined || v === "" ? "—" : inrFmt.format(v));
const num = (v) => (v === null || v === undefined ? "—" : Number(v).toLocaleString("en-IN", { maximumFractionDigits: 2 }));
const WORD = { APPROVE: "Approved", REVIEW: "Review", REJECT: "Rejected" };
const STAMP = { APPROVE: "APPROVED", REVIEW: "ON HOLD", REJECT: "REJECTED" };
const FIELD_NAMES = {
  vendor_name: "Vendor name", vendor_gstin: "Vendor GSTIN", buyer_gstin: "Billed-to GSTIN", invoice_number: "Invoice number",
  invoice_date: "Invoice date", due_date: "Due date",
  po_number: "PO number", subtotal: "Subtotal (pre-tax)", total: "Total", bank: "Pay to (bank)",
};

const state = { samples: [], runId: null, poll: null, open: new Set(), timer: null, started: 0, filter: "ALL", lastStatus: {} };

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return r.json();
}
function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast.h); toast.h = setTimeout(() => (t.hidden = true), 3200);
}
function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso.length === 10 ? iso + "T00:00:00" : iso);
  return d.toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" });
}
function fmtTime(iso) {
  const d = new Date(iso);
  return d.toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
}

/* ---------------------------------------------------------------- navigation */
function show(view) {
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.view === view)));
  ["waiting", "process", "dashboard", "payments", "records"].forEach((v) => ($("#view-" + v).hidden = v !== view));
  if (view === "waiting") loadWaiting(); else stopWaiting();
  if (view === "dashboard") loadDashboard();
  if (view === "payments") loadPayments();
  if (view === "records") loadRecords();
  history.replaceState(null, "", view === "process" && state.runId ? "#run=" + state.runId : "#" + view);
}
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));

/* ---------------------------------------------------------------- inbox */
async function loadSamples() {
  state.samples = await api("/api/samples");
  const groups = [["happy", "Happy path"], ["edge", "Edge cases"], ["fraud", "Fraud attempts"], ["learning", "Learns from people"], ["language", "Languages"], ["extra", "Also try"]];
  $("#samples").innerHTML = groups.map(([k, label]) => {
    const items = state.samples.filter((s) => s.kind === k);
    if (!items.length) return "";
    return `<div class="group-title">${label}</div>` + items.map((s) => `
      <div class="sample" role="button" tabindex="0" data-file="${esc(s.file)}" title="${esc(s.story)}">
        <span class="sample-title">${esc(s.title)}</span>
        <a class="sample-dl" href="/api/samples/${encodeURIComponent(s.file)}" target="_blank" rel="noopener" title="Open the PDF">PDF</a>
        <span class="sample-meta">${esc(s.vendor)}, ${inr(s.total)}</span>
      </div>`).join("");
  }).join("");
  document.querySelectorAll(".sample").forEach((el) => {
    const go = (e) => { if (e.target.closest(".sample-dl")) return; startRun({ sample: el.dataset.file }); };
    el.addEventListener("click", go);
    el.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(e); } });
  });
}
function markActive(file) {
  document.querySelectorAll(".sample").forEach((el) => el.classList.toggle("active", el.dataset.file === file));
}

const drop = $("#drop");
$("#file").addEventListener("change", (e) => { if (e.target.files[0]) startRun({ file: e.target.files[0] }); e.target.value = ""; });
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => { const f = e.dataTransfer.files[0]; if (f) startRun({ file: f }); });

/* ---------------------------------------------------------------- runs */
async function startRun({ sample, file }) {
  const fd = new FormData();
  if (file) fd.append("file", file); else fd.append("sample", sample);
  try {
    const { id } = await api("/api/runs", { method: "POST", body: fd });
    state.open = new Set(); state.lastStatus = {};
    show("process");
    openRun(id);
    markActive(sample || null);
    if (window.matchMedia("(max-width: 820px)").matches) $(".run").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) { toast(e.message); }
}

function openRun(id) {
  state.runId = id;
  state.pinned = null;
  clearInterval(state.poll); clearInterval(state.timer);
  $("#run-empty").hidden = true; $("#run").hidden = false;
  history.replaceState(null, "", "#run=" + id);
  let firstPaint = true;
  const tick = async () => {
    let run;
    try { run = await api("/api/runs/" + id); } catch (e) { clearInterval(state.poll); toast(e.message); return; }
    if (state.runId !== id) return;
    renderRun(run, firstPaint);
    firstPaint = false;
    if (run.state !== "running") { clearInterval(state.poll); clearInterval(state.timer); refreshBadge(); }
  };
  state.started = Date.now();
  state.timer = setInterval(() => { $("#run-timer").textContent = ((Date.now() - state.started) / 1000).toFixed(1) + " s"; }, 100);
  tick();
  state.poll = setInterval(tick, 300);
}

function renderRun(run, firstPaint) {
  markActive(run.sample);
  const res = run.result;
  $("#run-title").textContent = res?.invoice?.vendor || run.vendor || run.filename;
  const ch = run.channel || {};
  const via = ch.channel === "email" ? `Email from ${ch.sender || "unknown sender"}${ch.subject ? ` ("${ch.subject}")` : ""}`
    : ch.channel === "vendor portal" ? `Vendor upload form, ${ch.sender || ""}` : ch.channel === "upload" ? "Uploaded here" : "";
  const bits = via ? [via, run.filename] : [run.filename];
  if (run.invoice_number) bits.push("Invoice " + run.invoice_number);
  if (run.total) bits.push(inr(run.total));
  $("#run-sub").textContent = bits.join("  /  ");
  if (run.state !== "running") {
    $("#run-timer").textContent = run.duration != null ? `Finished: ${run.duration.toFixed(2)} s processing` : "";
    $("#run-timer").title = run.wall ? `${run.wall.toFixed(1)} s on screen, including the pause between stages for the live view` : "";
  }
  if (run.state === "running" && firstPaint && run.created) state.started = Date.now();

  // document pane
  const paper = $("#paper");
  const hasImg = run.stages[0].status !== "pending" && run.stages[0].status !== "running";
  if (hasImg && paper.dataset.run !== run.id) {
    paper.dataset.run = run.id;
    paper.innerHTML = `<img alt="Page 1 of ${esc(run.filename)}" src="/api/runs/${run.id}/preview.png"><div class="hl" hidden></div>`;
    const img = paper.querySelector("img");
    img.addEventListener("load", () => { paper.style.aspectRatio = `${img.naturalWidth} / ${img.naturalHeight}`; });
  } else if (!hasImg && paper.dataset.run !== run.id + ":wait" && paper.dataset.run !== run.id) {
    paper.dataset.run = run.id + ":wait";
    paper.innerHTML = `<div class="paper-empty">Receiving…</div>`;
    paper.style.aspectRatio = "";
  }
  paper.classList.toggle("scanning", run.state === "running");
  const src = run.source === "ocr" ? "Scanned image, read with OCR" : run.source ? "Digital PDF, read from its text layer" : "";
  $("#doc-caption").innerHTML = src ? `${src}. <a href="/api/runs/${run.id}/file.pdf" target="_blank" rel="noopener">Open original PDF</a>` : "";
  const pending = res && res.awaiting_human && !run.resolution && res.decision !== "REVIEW";
  const stampWord = res ? (run.resolution ? STAMP[run.resolution.action] : pending ? "AGENT: " + res.decision : STAMP[res.decision]) : "";
  const old = paper.querySelector(".stamp");
  if (old && old.dataset.word !== stampWord) old.remove();
  if (res && !paper.querySelector(".stamp")) {
    const s = document.createElement("div");
    s.className = "stamp " + (run.resolution ? run.resolution.action : res.decision);
    s.dataset.word = stampWord;
    s.innerHTML = `<span class="stamp-word">${stampWord}</span><span class="stamp-sub">AP DESK  ${fmtDate(run.created).toUpperCase()}${res.invoice?.repaired ? "  OCR CHECKED" : ""}</span>`;
    paper.appendChild(s);
  }

  // decision card
  $("#decision").innerHTML = res ? decisionCard(run) : run.state === "error" ? `<div class="decision REJECT"><span class="decision-word">Process error</span><h2>${esc(run.error)}</h2><p class="muted">The run stopped. Nothing was posted. Try another file, or check the server log.</p></div>` : "";
  bindResolve(run);
  bindReply(run);
  bindCorrection(run);

  // stages
  const ol = $("#stages");
  ol.innerHTML = run.stages.map((s, i) => {
    const prev = state.lastStatus[s.key];
    if (prev && prev !== s.status && (s.status === "warn" || s.status === "fail")) state.open.add(s.key);
    if (s.key === "decision" && s.status !== "pending" && s.status !== "running" && prev === "running") state.open.add("decision");
    state.lastStatus[s.key] = s.status;
    const open = state.open.has(s.key);
    const ms = s.started && s.ended ? Math.max(1, Math.round((s.ended - s.started) * 1000)) : null;
    const summary = s.status === "pending" ? s.explain : s.status === "running" ? "Working…" :
      (s.findings.find((f) => f.status === "fail") || s.findings.find((f) => f.status === "warn") ||
       (s.key === "decision" ? s.findings[0] : s.findings.filter((f) => f.status === "info").pop()) || s.findings[0] || {}).text || "";
    const done = !["pending", "running"].includes(s.status);
    return `<li class="stage ${done ? "done" : ""}" data-status="${s.status}" data-key="${s.key}">
      <span class="marker" aria-hidden="true">${{ pass: "✓", info: "i", warn: "!", fail: "✕", error: "✕" }[s.status] || i + 1}</span>
      <div class="stage-card">
        <button class="stage-row" ${done ? "" : "disabled"} aria-expanded="${open}">
          <span class="stage-label">${esc(s.label)}</span>
          <span class="stage-time">${ms !== null ? ms + " ms" : ""}</span>
          <span class="stage-summary">${esc(summary)}</span>
        </button>
        ${open && done ? `<div class="stage-body">${stageBody(s, run)}</div>` : ""}
      </div></li>`;
  }).join("");
  ol.querySelectorAll(".stage-row").forEach((b) => b.addEventListener("click", () => {
    const key = b.closest(".stage").dataset.key;
    state.open.has(key) ? state.open.delete(key) : state.open.add(key);
    renderRun(run, false);
  }));
}

function decisionCard(run) {
  const r = run.result, inv = r.invoice || {};
  const history = (run.revisions || []).length ? `<div class="history">${run.revisions.map((v) =>
      `<div><span class="chip ${v.decision}">${WORD[v.decision] || v.decision}</span> then re-checked: ${esc(v.reason)}</div>`).join("")}</div>` : "";
  const reasons = (r.handoff || []).length ? `<p class="subhead">${r.decision === "REJECT" ? "Why it was stopped, and who acts" :
      `${r.handoff.length} question${r.handoff.length > 1 ? "s" : ""} for people`}</p>
    <div class="handoffs">${r.handoff.map(handoffCard).join("")}</div>`
    : r.reasons.length ? `<ul class="reasons">${r.reasons.map((x) => `<li><span class="tag">${esc(stageName(x.stage))}</span>${esc(x.text)}</li>`).join("")}</ul>` : "";
  const notes = r.notes.length ? `<details><summary>${r.notes.length} note${r.notes.length > 1 ? "s" : ""} for the record</summary>
    <ul class="reasons">${r.notes.map((x) => `<li><span class="tag">${esc(stageName(x.stage))}</span>${esc(x.text)}</li>`).join("")}</ul></details>` : "";
  const email = r.vendor_email ? `<details><summary>Draft message to vendor</summary>
    <div class="email"><b>To:</b> ${esc(r.vendor_email.to)}\n<b>Subject:</b> ${esc(r.vendor_email.subject)}\n\n${esc(r.vendor_email.body)}</div>
    <p class="muted" style="font-size:.8rem;margin-top:4px">${esc(r.vendor_email.note)}</p></details>` : "";
  const res = run.resolution;
  const sensitive = (r.handoff || []).some((h) => ["BANK_CHANGE", "IMPERSONATION", "FRAUD_HIGH"].includes(h.code));
  const resolve = (r.decision === "REVIEW" || r.awaiting_human || res) ? (res
    ? `<div class="resolved"><b>${res.action === "APPROVE" ? "Approved" : "Rejected"} by ${esc(res.by)}</b>${res.via ? ` via ${esc(res.via)}` : ""} on ${fmtTime(res.at)}${res.note ? `: “${esc(res.note)}”` : ""}</div>`
    : `<div class="resolve"><input id="resolve-note" placeholder="${r.decision === "REJECT" ? "Why approve anyway? (required to approve)" : "Reviewer note (optional)"}" aria-label="Reviewer note">
        ${sensitive ? `<label class="verify"><input type="checkbox" id="resolve-verified"> I called the vendor on the number in the vendor master</label>` : ""}
        <button class="btn approve small" data-act="approve">${r.decision === "REJECT" ? "Approve anyway" : "Approve payment"}</button>
        <button class="btn reject small" data-act="reject">Reject invoice</button></div>`) : "";
  return `<div class="decision ${r.decision}">
    <span class="decision-word">${res ? WORD[res.action] + " by " + esc(res.by) : r.awaiting_human ? "Agent recommends: " + ({ APPROVE: "approve", REVIEW: "hold", REJECT: "reject" }[r.decision]) : WORD[r.decision]}</span>
    <h2>${esc(r.handoff?.length ? r.handoff[0].category + (r.handoff.length > 1 ? ` and ${r.handoff.length - 1} more` : "") : r.headline.replace(/^(Approved|Needs review|Rejected): /, ""))}</h2>
    <dl>
      <dt>Next step</dt><dd>${esc(r.next_action)}</dd>
      <dt>Routed to</dt><dd>${esc(r.route_to)}</dd>
      <dt>Invoice</dt><dd>${esc(inv.invoice_number || "—")}, ${fmtDate(inv.invoice_date)}, ${inr(inv.total)} (${inr(inv.subtotal)} + ${inr(inv.tax)} tax)</dd>
      ${inv.tds ? `<dt>Pay</dt><dd>${inr(inv.net_payable)} after TDS of ${inr(inv.tds)}</dd>` : ""}
      ${r.erp_doc ? `<dt>ERP</dt><dd>Posted as <b>${esc(r.erp_doc)}</b> and read back</dd>` : ""}
      <dt>PO</dt><dd>${esc(inv.po_number || "none")}${inv.po_how === "inferred" ? " (suggested, not on invoice)" : ""}</dd>
    </dl>
    ${history}${reasons}${correction(run)}${conversation(run)}${notes}${email}${resolve}
  </div>`;
}
function correction(run) {
  const r = run.result;
  if (r.decision !== "REVIEW" || run.resolution) return "";
  const ex = (run.stages.find((s) => s.key === "extract") || {}).data || {};
  const missing = (ex.fields || []).filter((f) => ["invoice_number", "invoice_date", "due_date", "po_number"].includes(f.field) && f.value == null && f.field !== "due_date");
  if (!missing.length) return "";
  return `<div class="sim"><p class="subhead">Type what the reader missed <span class="muted hint">It learns where this vendor prints it</span></p>
    ${missing.map((f) => `<label class="fix"><span>${esc(FIELD_NAMES[f.field])}</span>
      <input name="${f.field}" type="${f.field.includes("date") ? "date" : "text"}" placeholder="as printed on the invoice"></label>`).join("")}
    <button class="btn small" id="fix-send">Save and re-check</button></div>`;
}
function bindCorrection(run) {
  $("#fix-send")?.addEventListener("click", async () => {
    const fd = new FormData();
    document.querySelectorAll(".fix input").forEach((i) => { if (i.value) fd.append(i.name, i.value); });
    $("#fix-send").disabled = true;
    try {
      const j = await api(`/api/runs/${run.id}/correct`, { method: "POST", body: fd });
      toast(j.learned.length ? "Learned: " + j.learned.map((l) => `'${l.label}' is the ${l.field.replace("_", " ")}`).join(", ") : "Saved. Re-checking.");
      state.open = new Set(["decision"]); state.lastStatus = {}; openRun(run.id);
    } catch (e) { toast(e.message); $("#fix-send").disabled = false; }
  });
}
function conversation(run) {
  const t = run.thread || [];
  const ex = run.reply_examples || [];
  if (!t.length && !ex.length) return "";
  const who = (m) => m.direction === "out" ? `Agent → ${m.to}` : `${m.sender || m.party.replace("internal:", "")} → Agent`;
  const items = t.map((m) => `<li class="msg ${m.direction}">
      <div class="msg-head"><b>${esc(who(m))}</b><span class="muted">${fmtTime(m.at)}${m.direction === "out" ? (m.status === "sent" ? ", sent" : ", logged (no mail connection)") : ""}</span></div>
      <div class="msg-subj">${esc(m.subject || "")}</div>
      <details${m.direction === "in" ? " open" : ""}><summary>${m.direction === "in" ? "Reply" : "Message"}</summary><div class="msg-body">${esc(m.body || "")}</div></details>
      ${m.parsed ? `<div class="msg-parsed">Understood as <b>${esc(m.parsed.intent.replace(/_/g, " "))}</b>${m.parsed.po ? ` (${esc(m.parsed.po)})` : ""}${m.parsed.qty ? ` (${m.parsed.qty})` : ""}, read by ${m.parsed.how === "llm" ? "the LLM" : "rules"}${m.outcome ? `. ${esc(m.outcome)}` : ""}</div>` : ""}
    </li>`).join("");
  const sim = ex.length ? `<div class="sim"><p class="subhead">Simulate a reply <span class="muted hint">In production these arrive by email through n8n</span></p>
      <div class="sim-btns">${ex.map((e, i) => `<button class="btn ghost small" data-ex="${i}">${esc(e.label)}</button>`).join("")}</div>
      <select id="sim-party" aria-label="Reply from">${[...new Set(ex.map((e) => e.party))].map((p) => `<option value="${esc(p)}">${esc(p === "vendor" ? "Vendor" : p.replace("internal:", ""))}</option>`).join("")}</select>
      <textarea id="sim-text" rows="2" aria-label="Reply text" placeholder="Type the reply…">${esc(ex[0].text)}</textarea>
      <button class="btn small" id="sim-send">Send reply to the agent</button></div>` : "";
  return `<p class="subhead">Conversation <span class="muted hint">The agent writes to the address on file and acts on replies</span></p><ul class="thread">${items}</ul>${sim}`;
}
function handoffCard(h) {
  const due = new Date(h.due_by);
  const dueTxt = due.toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
  return `<div class="handoff ${h.severity}">
    <div class="ho-top"><span class="ho-cat">${esc(h.category)}</span><span class="ho-owner">${esc(h.owner)}</span></div>
    <p class="ho-q">${esc(h.question)}</p>
    <ul class="ho-ev">${h.evidence.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>
    ${h.tried.length ? `<p class="ho-meta"><b>Already done:</b> ${h.tried.map(esc).join("; ")}</p>` : ""}
    <p class="ho-meta">Answer by <b>${dueTxt}</b> (${h.sla_hours} h), otherwise it escalates to ${esc(h.escalate_to)}.</p>
  </div>`;
}
function bindReply(run) {
  const ex = run.reply_examples || [];
  document.querySelectorAll(".sim [data-ex]").forEach((b) => b.addEventListener("click", () => {
    const e = ex[+b.dataset.ex]; $("#sim-text").value = e.text; $("#sim-party").value = e.party;
  }));
  $("#sim-send")?.addEventListener("click", async () => {
    const fd = new FormData(); fd.append("party", $("#sim-party").value); fd.append("body", $("#sim-text").value);
    $("#sim-send").disabled = true;
    try {
      const j = await api(`/api/runs/${run.id}/reply`, { method: "POST", body: fd });
      toast(j.outcome?.text || "Reply recorded");
      state.open = new Set(["decision"]); state.lastStatus = {};
      openRun(run.id);
    } catch (e) { toast(e.message); $("#sim-send").disabled = false; }
  });
}
function bindResolve(run) {
  document.querySelectorAll(".resolve [data-act]").forEach((b) => b.addEventListener("click", async () => {
    const fd = new FormData(); fd.append("action", b.dataset.act); fd.append("note", $("#resolve-note")?.value || "");
    fd.append("verified", $("#resolve-verified")?.checked ? "1" : "");
    document.querySelectorAll(".resolve button").forEach((x) => (x.disabled = true));
    try {
      const updated = await api(`/api/runs/${run.id}/resolve`, { method: "POST", body: fd });
      toast(b.dataset.act === "approve" ? "Approved. PO balance updated." : "Rejected. Removed from the payment queue.");
      renderRun(updated, false); refreshBadge();
    } catch (e) { toast(e.message); }
  }));
}
const stageName = (k) => ({ intake: "Intake", read: "Read", extract: "Extract", validate: "Self-check", vendor: "Vendor", duplicate: "Duplicate", po: "PO match", risk: "Fraud", decision: "Decision" }[k] || k);

/* ---------------------------------------------------------------- show-on-invoice highlights */
const boxAttr = (b) => (b ? ` data-box="${b.join(",")}" tabindex="0" title="Show on the invoice"` : "");
function highlight(boxStr, pin) {
  const hl = document.querySelector("#paper .hl");
  if (!hl) return;
  if (!boxStr) { if (!state.pinned) hl.hidden = true; return; }
  const [x0, y0, x1, y1] = boxStr.split(",").map(Number);
  Object.assign(hl.style, { left: x0 * 100 + "%", top: y0 * 100 + "%", width: (x1 - x0) * 100 + "%", height: (y1 - y0) * 100 + "%" });
  hl.hidden = false;
  if (pin) {
    state.pinned = boxStr;
    hl.classList.remove("flash"); void hl.offsetWidth; hl.classList.add("flash");
    if (window.matchMedia("(max-width: 1180px)").matches) $("#paper").scrollIntoView({ behavior: "smooth", block: "center" });
  }
}
document.addEventListener("mouseover", (e) => { const el = e.target.closest("[data-box]"); if (el) highlight(el.dataset.box); });
document.addEventListener("mouseout", (e) => {
  const el = e.target.closest("[data-box]");
  if (el && !el.contains(e.relatedTarget)) highlight(state.pinned || null);
});
document.addEventListener("click", (e) => { const el = e.target.closest("[data-box]"); if (el) highlight(el.dataset.box, true); });
document.addEventListener("keydown", (e) => {
  const el = e.target.closest?.("[data-box]");
  if (el && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); highlight(el.dataset.box, true); }
});

/* ---------------------------------------------------------------- stage detail renderers */
function findingsList(fs) {
  return `<ul class="findings">${fs.map((f) => `<li class="finding"><span class="dot ${f.status}" aria-label="${f.status}"></span><span>${esc(f.text)}</span></li>`).join("")}</ul>`;
}
function confBar(c) {
  const cls = c >= 0.85 ? "" : c >= 0.6 ? "mid" : "low";
  return `<span class="conf ${cls}"><i style="width:${Math.round(c * 100)}%"></i></span>${Math.round(c * 100)}%`;
}
function table(head, rows, numCols = [], rowAttrs = []) {
  return `<div class="tbl-wrap"><table><thead><tr>${head.map((h, i) => `<th class="${numCols.includes(i) ? "num" : ""}">${h}</th>`).join("")}</tr></thead>
    <tbody>${rows.map((r, k) => `<tr${rowAttrs[k] ? ` class="locatable"${rowAttrs[k]}` : ""}>${r.map((c, i) => `<td class="${numCols.includes(i) ? "num" : ""}">${c}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}
function stageBody(s, run) {
  const d = s.data || {};
  let extra = "";
  if (s.key === "intake") {
    extra = d.pages ? `<p class="muted" style="font-size:.82rem">Fingerprint ${esc(d.sha256)}…, ${num(d.text_chars)} characters in text layer</p>` : "";
  } else if (s.key === "read") {
    extra = d.text ? `<details><summary>Show the text read from the page</summary><pre class="ocr">${esc(d.text)}</pre></details>` : "";
  } else if (s.key === "extract" && d.fields) {
    const fields = d.fields.filter((f) => f.field !== "bank" || f.value != null);
    extra = `<p class="subhead">Header fields <span class="muted hint">Click a row to see where it is on the invoice</span></p>` + table(["Field", "Value", "Found by", "Confidence"],
      fields.map((f) => [esc(FIELD_NAMES[f.field] || f.field),
        f.value == null ? `<span class="bad">not found</span>` : esc(typeof f.value === "number" ? inr(f.value) : f.value),
        `<span class="muted">${esc(f.method)}</span>`, f.value == null ? "" : confBar(f.confidence)]), [], fields.map((f) => boxAttr(f.box)));
    if (d.lines?.length) {
      extra += `<p class="subhead">Line items${d.tax_inclusive ? " (prices include GST)" : ""}</p>` + table(["Description", "Qty", "Rate", "Amount"],
        d.lines.map((l) => [esc(l.description), num(l.qty), inr(l.unit_price), inr(l.amount)]), [1, 2, 3], d.lines.map((l) => boxAttr(l.box)));
    }
    if (d.taxes?.length) extra += table(["Tax", "Rate", "Amount"], d.taxes.map((t) => [esc(t.type), t.rate != null ? t.rate + "%" : "—", inr(t.amount)]), [1, 2], d.taxes.map((t) => boxAttr(t.box)));
    if (d.agreement) extra += `<p class="subhead">Rules reader vs LLM reader</p>` + table(["Field", "Rules", "LLM", ""],
      d.agreement.map((a) => [esc(FIELD_NAMES[a.field] || a.field), esc(a.rules ?? "—"), esc(a.llm ?? "—"), a.agree ? `<span class="ok">✓</span>` : `<span class="bad">≠</span>`]));
  } else if (s.key === "validate" && d.identities) {
    const before = d.before ? Object.fromEntries(d.before.map((x) => [x.id, x])) : null;
    if (d.repair) {
      extra += `<div class="callout locatable"${boxAttr(d.repair.box)}><b>Repaired:</b> ${esc(d.repair.cell)} <span class="strike">${inr(d.repair.old)}</span> → <b>${inr(d.repair.new)}</b>.
        Confirmed by: ${d.repair.corroborated_by.map(esc).join("; ")}.</div>`;
    }
    extra += table(["Check", "Expected", "On invoice", before ? "Before repair" : "", "Result"],
      d.identities.map((x) => [esc(x.label), inr(x.expected), inr(x.found),
        before ? (before[x.id]?.ok ? `<span class="ok">✓</span>` : `<span class="bad">✕</span> <span class="muted" style="font-size:.8rem">${inr(before[x.id]?.found)} vs ${inr(before[x.id]?.expected)}</span>`) : "",
        x.ok ? `<span class="ok">✓</span>` : `<span class="bad">✕</span>`]), [1, 2]);
  } else if (s.key === "vendor" && d.vendor) {
    const v = d.vendor;
    extra = table(["Vendor", "GSTIN", "Status", "Terms", "Bank"], [[`${esc(v.id)} ${esc(v.name)}`, esc(v.gstin),
      v.status === "approved" ? `<span class="ok">approved</span>` : `<span class="bad">${esc(v.status)}</span>`, `Net ${v.terms_days}`, esc(v.bank)]]);
  } else if (s.key === "risk" && d.signals) {
    const lvl = d.level, pct = d.score;
    extra = `<div class="risk"><div class="risk-meter ${lvl}" role="img" aria-label="Risk score ${pct} of 100"><span style="width:${Math.max(2, pct)}%"></span></div>
      <p class="risk-label"><b>Risk ${pct}/100</b> <span class="muted">(${lvl}: 70+ blocks payment, a single heavy signal holds it)</span></p></div>`;
    if (d.bank || d.bank_on_file) extra += table(["", "Bank account"], [["On the invoice", esc(d.bank || "not printed")], ["In vendor master", esc(d.bank_on_file || "—")]], [], [boxAttr(d.bank_box), ""]);
    if (d.signals.length) extra += `<p class="subhead">Signals</p>` + table(["Signal", "Weight", "Detail"], d.signals.map((g) => [esc(g.label), g.weight ? "+" + g.weight : "0", `<span class="muted">${esc(g.detail)}</span>`]), [1]);
  } else if (s.key === "post" && d.journal) {
    extra = `<p class="subhead">Journal entry ${d.doc_no ? `${esc(d.doc_no)} ` : ""}<span class="muted hint">${esc(d.adapter || "")}</span></p>` +
      table(["Account", "", "Debit", "Credit"], d.journal.lines.map((l) => [esc(l.account), esc(l.name) + (l.memo ? `<br><span class="muted" style="font-size:.78rem">${esc(l.memo)}</span>` : ""),
        l.debit ? inr(l.debit) : "", l.credit ? inr(l.credit) : ""]).concat([["", "<b>Total</b>", `<b>${inr(d.journal.debit)}</b>`, `<b>${inr(d.journal.credit)}</b>`]]), [2, 3]);
    if (d.readback) extra += `<p class="subhead">Read back from the ERP</p><div class="checks">${d.readback.map((c) => `<span class="${c.ok ? "ok" : "bad"}">${c.ok ? "✓" : "✕"} ${esc(c.check)}</span>`).join("")}</div>`;
    if (d.journal.coding) extra += `<p class="subhead">GL coding <span class="muted hint">Change an account to teach it for next time</span></p>` + table(["Line", "Account", "Decided by"], d.journal.coding.map((c) => [esc(c.line),
      `<select class="gl-pick" data-desc="${esc(c.line)}" data-vendor="${esc(run.result.invoice.vendor_id || "")}" aria-label="GL account">${Object.entries(state.accounts || {}).map(([k, v]) => `<option value="${k}" ${k === c.account ? "selected" : ""}>${k} ${esc(v)}</option>`).join("") || `<option>${esc(c.account)} ${esc(c.name)}</option>`}</select>`,
      `<span class="muted">${esc(c.how)}</span>`]));
  } else if (s.key === "tax" && d.itc) {
    const rows = [];
    rows.push(["E-invoice (IRN)", d.irn ? `<span class="mono-ish">${esc(d.irn.slice(0, 16))}…</span>` : `<span class="muted">none printed</span>`]);
    (d.tds || []).forEach((t) => rows.push([esc(t.section), t.amount ? `${inr(t.amount)} <span class="muted">(${t.rate}% of ${inr(t.base)})</span>` : `<span class="muted">none: below threshold (${inr(t.base)})</span>`]));
    rows.push(["Pay to vendor", `<b>${inr(d.net_payable)}</b>${d.tds_total ? ` <span class="muted">(invoice total less TDS)</span>` : ""}`]);
    if (d.msme_deadline) rows.push(["MSME legal deadline", fmtDate(d.msme_deadline)]);
    rows.push(["Input tax credit", `${inr(d.itc.amount)} ${d.itc.in_gstr2b ? `<span class="ok">in GSTR-2B</span>` : `<span class="muted">pending (not in GSTR-2B yet)</span>`}`]);
    extra = table(["", ""], rows);
  } else if (s.key === "duplicate" && d.normalized) {
    extra = `<p class="muted" style="font-size:.82rem">Compared as “${esc(d.normalized)}” (case, punctuation and leading zeros removed), so INV-0042, inv 42 and INV42 are the same number.</p>`;
  } else if (s.key === "po" && d.po) {
    const p = d.po, sub = d.subtotal || 0;
    const usedPct = Math.min(100, (p.used / p.amount) * 100);
    const room = Math.max(0, p.remaining);
    const thisPct = Math.min(100 - usedPct, (Math.min(sub, room) / p.amount) * 100);
    const overPct = Math.min(100 - usedPct - thisPct, (Math.max(0, sub - room) / p.amount) * 100);
    extra = `<p class="subhead">${esc(p.po_number)}${p.how === "inferred" ? " (suggested match)" : ""}: ${inr(p.amount)} raised ${fmtDate(p.date)}</p>
      <div class="balance" role="img" aria-label="PO balance"><span class="used" style="width:${usedPct}%"></span><span class="this" style="width:${thisPct}%"></span><span class="over" style="width:${overPct}%"></span></div>
      <div class="legend"><span><i style="background:var(--muted)"></i>Billed before ${inr(p.used)}</span><span><i style="background:var(--carbon)"></i>This invoice ${inr(sub)}</span>${sub > room + 1 ? `<span><i style="background:var(--reject)"></i>Over by ${inr(sub - room)}</span>` : `<span>Left after ${inr(room - sub)}</span>`}</div>`;
    if (p.confirmed_by) extra += `<p class="muted" style="font-size:.84rem">PO ${esc(p.confirmed_by)}.</p>`;
    if (p.lines?.length) extra += `<p class="subhead">Line by line (PO, invoice and goods receipt)</p>` + table(["Invoice line", "Qty / left on PO", "Received", "Rate / PO rate", "Result"],
      p.lines.map((l) => [esc(l.invoice) + (l.po_line && l.po_line !== l.invoice ? `<br><span class="muted" style="font-size:.8rem">PO: ${esc(l.po_line)}</span>` : ""),
        l.po_line ? `${num(l.qty)} / ${num(l.qty_left)}` : "—", l.service ? `<span class="muted">service</span>` : l.received != null ? num(l.received) : "—", l.po_line ? `${inr(l.rate)} / ${inr(l.po_rate)}` : "—",
        `<span class="${l.status === "warn" ? "bad" : l.status === "pass" ? "ok" : ""}">${esc(l.note)}</span>`]), [1, 2, 3]);
    if (d.candidates?.length && p.how === "inferred") extra += `<p class="subhead">Candidate POs</p>` + table(["PO", "Match score", "Left on PO"], d.candidates.map((c) => [esc(c.po), c.score.toFixed(2), inr(c.remaining)]), [1, 2]);
  }
  return `<p class="stage-explain">${esc(s.explain)}</p>` + findingsList(s.findings) + extra;
}

/* ---------------------------------------------------------------- dashboard */
async function loadDashboard() {
  const d = await api("/api/dashboard");
  const c = d.counts;
  $("#dash-summary").innerHTML = d.total_runs ? `
    <p class="dash-sentence">${d.total_runs} invoice${d.total_runs > 1 ? "s" : ""} processed. <b class="a">${c.APPROVE} ${d.human_approval ? "passed every check" : "approved automatically"}</b>${d.auto_rate != null ? ` (${d.auto_rate}%)` : ""},
      <b class="r">${c.REVIEW} sent to review</b>${d.open_reviews ? ` (${d.open_reviews} still open)` : ""}, <b class="x">${c.REJECT} rejected</b>.</p>
    <div class="stats">
      ${d.human_approval ? `<div class="stat"><span>Waiting for your OK</span><strong>${d.waiting_count} (${inr(d.waiting_value)})</strong></div>` : ""}
      <div class="stat"><span>${d.human_approval ? "Agent needed no help" : "Straight-through rate"}</span><strong>${d.metrics.stp_rate}%</strong></div>
      <div class="stat"><span>Human questions per invoice</span><strong>${d.metrics.touches_per_invoice}</strong></div>
      <div class="stat"><span>${d.human_approval ? "Approved by people" : "Approved for payment"}</span><strong>${inr(d.approved_value)}</strong></div>
      <div class="stat"><span>Stopped from going out</span><strong>${inr(d.blocked_value)}</strong></div>
      <div class="stat"><span>Fraud signals caught</span><strong>${d.metrics.fraud_flags} (${inr(d.metrics.fraud_value)})</strong></div>
      <div class="stat"><span>OCR misreads repaired</span><strong>${d.metrics.ocr_repairs}</strong></div>
      <div class="stat"><span>Average processing time</span><strong>${d.avg_seconds ?? "—"} s</strong></div>
    </div>` : `<p class="dash-sentence">No invoices processed yet.</p><p class="muted">Process one from the inbox and it will show up here.</p>`;

  const ex = d.metrics?.exceptions || [];
  const maxEx = Math.max(1, ...ex.map((e) => e.count));
  $("#exceptions").innerHTML = ex.length ? `<ul class="bars">${ex.map((e) => `<li>
      <span class="bar-label">${esc(e.category)}<span class="muted">, ${esc(e.owner)}</span></span>
      <span class="bar"><i class="${e.fail ? "fail" : "warn"}" style="width:${(e.count / maxEx) * 100}%"></i></span>
      <span class="bar-n">${e.count}</span></li>`).join("")}</ul>`
    : `<p class="empty-note">No exceptions yet. Every invoice so far went straight through.</p>`;

  const q = d.runs.filter((r) => r.decision === "REVIEW" && !r.resolution);
  $("#queue").innerHTML = q.length ? q.map((r) => `<div class="queue-item">
      <div class="q-top"><b>${esc(r.vendor || r.filename)}</b><span>${inr(r.total)}</span></div>
      <p>${esc((r.headline || "").replace(/^Needs review: /, ""))}</p>
      <div><button class="btn ghost small" data-open="${r.id}">Open and decide</button></div></div>`).join("")
    : `<p class="empty-note">Nothing waiting. Invoices that need a person show up here.</p>`;

  $("#balances").innerHTML = table(["PO", "Billed / value", ""], d.balances.map((b) => {
    const u = (b.billed / b.amount) * 100, pend = Math.min(100 - u, (b.pending / b.amount) * 100);
    return [`<b>${esc(b.po_number)}</b><br><span class="muted" style="font-size:.8rem">${esc(b.vendor)}</span>`,
      `${inr(b.billed)}<br><span class="muted" style="font-size:.8rem">of ${inr(b.amount)}</span>`,
      `<div class="balance" style="min-width:90px" role="img" aria-label="${Math.round(u)}% billed"><span class="used" style="width:${u}%"></span><span class="pending" style="width:${pend}%"></span></div>`];
  }), [1]) + `<div class="legend"><span><i style="background:var(--muted)"></i>Billed (approved)</span><span><i style="background:var(--review)"></i>In review</span></div>`;

  const rows = d.runs.filter((r) => state.filter === "ALL" || ((r.resolution?.action) || r.decision) === state.filter);
  $("#history").innerHTML = rows.length ? `<div class="tbl-wrap"><table><thead><tr><th>When</th><th>Vendor</th><th>Invoice</th><th class="num">Total</th><th>Decision</th><th>Reason</th><th class="num">Time</th></tr></thead><tbody>
    ${rows.map((r) => {
      const dec = r.state === "running" ? `<span class="chip running">Running</span>` : r.state === "error" ? `<span class="chip error">Error</span>` :
        `<span class="chip ${r.decision}">${WORD[r.decision]}</span>${r.resolution ? ` <span class="chip ${r.resolution.action}">→ ${WORD[r.resolution.action]}</span>` : ""}`;
      return `<tr class="clickable" data-open="${r.id}" tabindex="0"><td>${fmtTime(r.created)}</td><td>${esc(r.vendor || "—")}<br><span class="muted" style="font-size:.78rem">${esc(r.filename)}</span></td>
        <td>${esc(r.invoice_number || "—")}</td><td class="num">${inr(r.total)}</td><td>${dec}</td>
        <td style="max-width:360px">${esc((r.headline || "").replace(/^(Approved|Needs review|Rejected): /, ""))}</td><td class="num">${r.duration != null ? r.duration.toFixed(2) + " s" : ""}</td></tr>`;
    }).join("")}</tbody></table></div>` : `<p class="empty-note">No runs match this filter.</p>`;

  try {
    const e = await api("/api/eval");
    $("#accuracy").innerHTML = e.invoices ? `<div class="stats">
        <div class="stat"><span>Decision accuracy</span><strong>${e.decision_accuracy}%</strong></div>
        <div class="stat"><span>False approvals</span><strong class="${e.false_approvals ? "bad" : "ok"}">${e.false_approvals}</strong></div>
        <div class="stat"><span>Silent errors</span><strong class="${e.silent_errors ? "bad" : "ok"}">${e.silent_errors}</strong></div>
        <div class="stat"><span>Good invoices sent to a person</span><strong>${e.missed_approvals} of ${e.good_invoices}</strong></div>
        <div class="stat"><span>Digit errors the arithmetic catches</span><strong>${e.checker.rate_rupee_or_more}%</strong></div>
        <div class="stat"><span>Invoices tested</span><strong>${e.invoices}</strong></div></div>` +
      table(["Situation", "Should be", "Right"], e.situations.map((x) => [esc(x.description), `<span class="chip ${x.expected}">${WORD[x.expected]}</span>`, `${x.correct}/${x.n}`]), [2])
      : `<p class="empty-note">No evaluation yet. Run <code>python scripts/evaluate.py</code>.</p>`;
  } catch (_) {}
  try {
    const ev = await api("/api/events?limit=40");
    const icon = { received: "In", decided: "Out", notify: "Msg", resolved: "Done", escalated: "Esc", skipped: "Skip" };
    $("#activity").innerHTML = ev.length ? `<ul class="feed">${ev.map((e) => `<li class="ev ${e.kind}">
        <span class="ev-k">${icon[e.kind] || e.kind}</span><span class="ev-t">${esc(e.text)}</span>
        <span class="ev-at">${fmtTime(e.at)}</span></li>`).join("")}</ul>`
      : `<p class="empty-note">Nothing yet. Emails received, messages sent, link approvals and escalations appear here.</p>`;
  } catch (_) {}

  document.querySelectorAll("[data-open]").forEach((el) => {
    const go = () => { state.open = new Set(["decision"]); state.lastStatus = {}; show("process"); openRun(el.dataset.open); };
    el.addEventListener("click", go);
    el.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
  });
  refreshBadge();
}
document.querySelectorAll("#filters button").forEach((b) => b.addEventListener("click", () => {
  state.filter = b.dataset.f;
  document.querySelectorAll("#filters button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
  loadDashboard();
}));
async function refreshBadge(n) {
  if (n === undefined) {
    try { n = (await api("/api/waiting")).items.length; } catch (_) { return; }
  }
  const b = $("#review-badge"); b.hidden = !n; b.textContent = n || "";
}

document.addEventListener("change", async (e) => {
  if (!e.target.classList.contains("gl-pick")) return;
  const fd = new FormData(); fd.append("vendor_id", e.target.dataset.vendor); fd.append("description", e.target.dataset.desc); fd.append("account", e.target.value);
  try { await api("/api/gl/remember", { method: "POST", body: fd }); toast("Learned: this line from this vendor will be coded to " + e.target.value + " from now on."); }
  catch (err) { toast(err.message); }
});

/* ---------------------------------------------------------------- payments */
async function loadPayments() {
  const d = await api("/api/payments");
  const t = d.totals;
  $("#pay-summary").innerHTML = `<p class="dash-sentence">${inr(t.open)} approved and waiting to be paid${t.overdue ? `, <b class="x">${inr(t.overdue)} overdue</b>` : ""}${t.due_7 ? `, ${inr(t.due_7)} due within 7 days` : ""}.</p>`;
  $("#pay-open").innerHTML = d.open.length ? `<div class="tbl-wrap"><table><thead><tr><th></th><th>ERP document</th><th>Vendor</th><th>Invoice</th><th>Due</th><th class="num">TDS</th><th class="num">Pay</th></tr></thead><tbody>
    ${d.open.map((x) => `<tr><td><input type="checkbox" class="pay-pick" value="${esc(x.doc_no)}" aria-label="Select ${esc(x.doc_no)}" ${x.days_left <= 7 ? "checked" : ""}></td>
      <td>${esc(x.doc_no)}</td><td>${esc(x.vendor)}${x.msme ? ` <span class="chip REVIEW">MSME</span>` : ""}<br><span class="muted" style="font-size:.78rem">${esc(x.bank || "")}</span></td>
      <td>${esc(x.invoice_number)}<br><span class="muted" style="font-size:.78rem">${esc(x.po_number || "")}</span></td>
      <td>${fmtDate(x.due_date)}<br><span class="${x.days_left < 0 ? "bad" : "muted"}" style="font-size:.78rem">${x.days_left < 0 ? `${-x.days_left} days overdue` : `in ${x.days_left} days`}</span></td>
      <td class="num">${x.tds ? inr(x.tds) : ""}</td><td class="num"><b>${inr(x.payable)}</b></td></tr>`).join("")}</tbody></table></div>`
    : `<p class="empty-note">Nothing to pay. Approved invoices appear here after they are posted to the ERP.</p>`;
  const sync = () => { $("#make-batch").disabled = !document.querySelectorAll(".pay-pick:checked").length; };
  document.querySelectorAll(".pay-pick").forEach((c) => c.addEventListener("change", sync)); sync();
  $("#pay-batches").innerHTML = d.batches.length ? d.batches.map((b) => `<div class="queue-item">
      <div class="q-top"><b>${esc(b.id)}</b><span>${inr(b.total)}</span></div>
      <p>${b.docs.length} invoice(s): ${b.docs.map((x) => esc(x.vendor)).join(", ")}. <span class="chip ${b.status === "paid" ? "APPROVE" : "REVIEW"}">${esc(b.status)}</span></p>
      <div class="sim-btns"><a class="btn ghost small" href="/api/payments/batch/${encodeURIComponent(b.id)}.csv">Download bank file</a>
      ${b.status !== "paid" ? `<button class="btn ghost small" data-paid="${esc(b.id)}">Bank confirmed: mark paid</button>` : ""}</div></div>`).join("")
    : `<p class="empty-note">No batches yet. Select invoices above and create one.</p>`;
  document.querySelectorAll("[data-paid]").forEach((b) => b.addEventListener("click", async () => {
    try { await api(`/api/payments/batch/${encodeURIComponent(b.dataset.paid)}/paid`, { method: "POST" }); toast("Marked paid in the ERP."); loadPayments(); } catch (e) { toast(e.message); }
  }));
  $("#pay-tds").innerHTML = d.tds.length ? table(["Section", "Month", "Amount", "Deposit by"], d.tds.map((x) => [esc(x.section), esc(x.month), inr(x.amount), fmtDate(x.deposit_by)]), [2])
    : `<p class="empty-note">No TDS deducted yet.</p>`;
}
document.addEventListener("click", async (e) => {
  if (e.target.id !== "make-batch") return;
  const docs = [...document.querySelectorAll(".pay-pick:checked")].map((c) => c.value);
  const fd = new FormData(); fd.append("docs", docs.join(","));
  try { const b = await api("/api/payments/batch", { method: "POST", body: fd }); toast(`${b.id} created: ${inr(b.total)}. Bank file ready.`); loadPayments(); }
  catch (err) { toast(err.message); }
});

/* ---------------------------------------------------------------- records */
async function loadRecords() {
  const d = await api("/api/reference");
  try {
    const ig = await api("/api/integrations");
    $("#integrations").innerHTML = table(["Connection", "Status"], [
      ["Email and vendor form intake (n8n → POST /api/intake)", ig.intake_token ? `<span class="ok">protected by API key</span>` : `<span class="muted">open (set INTAKE_TOKEN to protect)</span>`],
      ["Decisions to Slack and email (app → n8n webhook)", ig.webhook ? `<span class="ok">connected</span> <span class="muted">${esc(ig.webhook_target)}…</span>` : `<span class="muted">not connected: messages are logged in Activity only</span>`],
      ["One-click approve / reject links", `<span class="ok">on</span> <span class="muted">signed, expire in 7 days, base ${esc(ig.public_url)}</span>`],
      ["Escalation timer (n8n → POST /api/escalations/run)", `<span class="muted">runs when n8n calls it, hourly</span>`],
    ]);
  } catch (_) {}
  try {
    const L = await api("/api/learning");
    $("#learned").innerHTML = (L.layouts.length ? table(["Vendor", "Field", "Label it learned", "Taught by"], L.layouts.map((x) => [esc(x.vendor_id), esc(FIELD_NAMES[x.field] || x.field), `“${esc(x.label)}” (value ${x.position === "right" ? "to the right" : "below"})`, `${esc(x.by)}, ${fmtTime(x.at)}`])) : `<p class="empty-note">No layouts learned yet. Correct a missing field on an invoice on hold and it appears here.</p>`)
      + (L.gl.length ? `<p class="subhead">GL codes</p>` + table(["Vendor | line", "Account"], L.gl.map((g) => [esc(g.key), `${esc(g.account)} ${esc(g.name || "")}`])) : "");
  } catch (_) {}
  $("#rules-intro").textContent = `Master data the process checks against. Buyer: ${d.buyer.name}, GSTIN ${d.buyer.gstin}. ` +
    `An invoice may exceed PO prices by at most ${d.rules.tolerance_pct}% or ${inr(d.rules.tolerance_abs)}, whichever is smaller.`;
  $("#vendors").innerHTML = table(["ID", "Vendor", "GSTIN", "State", "Status", "Terms", "Compliance", "Bank", "Phone on file"], d.vendors.map((v) => [esc(v.id), esc(v.name), esc(v.gstin), esc(v.state),
    v.status === "approved" ? `<span class="ok">approved</span>` : `<span class="bad">${esc(v.status)}</span><br><span class="muted" style="font-size:.78rem">${esc(v.block_reason || "")}</span>`,
    `Net ${v.terms_days}`, [v.einvoice ? "e-invoice" : "", v.msme ? "MSME" : "", v.tds ? `TDS: ${v.tds}` : ""].filter(Boolean).join(", ") || "—",
    esc(v.bank), esc(v.phone || "—")]));
  const vname = Object.fromEntries(d.vendors.map((v) => [v.id, v.name]));
  $("#pos").innerHTML = table(["PO", "Vendor", "Raised", "Lines", "Value"], d.pos.map((p) => [`<b>${esc(p.po_number)}</b>`, esc(vname[p.vendor_id]), fmtDate(p.date),
    p.lines.map((l) => `${esc(l.description)}: ${num(l.qty)} × ${inr(l.unit_price)}`).join("<br>"), inr(p.amount)]), [4]);
  $("#ledger").innerHTML = table(["Vendor", "Invoice", "Date", "PO", "Subtotal", "Total", "Status", "Source"], d.ledger.slice().reverse().map((l) => [esc(vname[l.vendor_id] || l.vendor_id),
    esc(l.invoice_number), fmtDate(l.invoice_date), esc(l.po_number || "—"), inr(l.subtotal), inr(l.total), `<span class="chip ${l.status}">${WORD[l.status] || l.status}</span>`,
    l.source === "history" ? "Before demo" : "This demo"]), [4, 5]);
}

/* ---------------------------------------------------------------- reset + boot */
$("#reset").addEventListener("click", async () => {
  if (!confirm("Clear all runs and restore the invoice ledger to its starting state?")) return;
  try {
    await api("/api/reset", { method: "POST" });
    state.runId = null; clearInterval(state.poll); clearInterval(state.timer);
    $("#run").hidden = true; $("#run-empty").hidden = false; $("#paper").innerHTML = `<div class="paper-empty">The invoice appears here</div>`;
    $("#paper").dataset.run = ""; $("#doc-caption").textContent = ""; markActive(null);
    toast("Demo data reset. Ledger restored to the starting state.");
    refreshBadge(0);
    if (!$("#view-dashboard").hidden) loadDashboard();
    if (!$("#view-records").hidden) loadRecords();
    if (!$("#view-waiting").hidden) loadWaiting();
  } catch (e) { toast(e.message); }
});

(async function boot() {
  try {
    const h = await api("/api/health");
    $("#reader-mode").textContent = h.llm ? `Readers: rules + ${h.llm}` : "Reader: rules + OCR";
  } catch (_) {}
  await loadSamples();
  try { state.accounts = (await api("/api/learning")).accounts; } catch (_) {}
  refreshBadge();
  const m = location.hash.match(/^#run=(.+)$/);
  if (m) { state.open = new Set(["decision"]); show("process"); openRun(m[1]); }
  else if (location.hash === "#dashboard") show("dashboard");
  else if (location.hash === "#records") show("records");
  else if (location.hash === "#payments") show("payments");
  else if (location.hash === "#process") show("process");
  else show("waiting");
})();
