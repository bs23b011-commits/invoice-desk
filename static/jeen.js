/* Jeen: the AP assistant panel. Slides in from the right on every tab.
   Uses helpers from app.js ($, esc, api, toast, show, openRun) and W from waiting.js.
   Jeen answers and prepares. Nothing is sent, opened or decided without the user clicking. */
"use strict";

const JEEN = {
  open: false,
  busy: false,
  messages: [],          // {role, content} - what we send back as memory
  turns: [],             // {role, text, sources, actions, tools, mode} - what is on screen
  mode: null,            // "llm" | "rules"
};

const J_ICON = {
  jeen: `<svg aria-hidden="true" viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor"
    stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">
    <path d="M4 12.5A7.5 7.5 0 0 1 11.5 5h1A7.5 7.5 0 0 1 20 12.5v0A7.5 7.5 0 0 1 12.5 20H8l-3.3 2.2a.5.5 0 0 1-.7-.6L5 18.4A7.5 7.5 0 0 1 4 12.5z"/>
    <circle cx="9.5" cy="12.5" r="1.1" fill="currentColor" stroke="none"/>
    <circle cx="14.5" cy="12.5" r="1.1" fill="currentColor" stroke="none"/></svg>`,
  send: `<svg aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor"
    stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12h15"/><path d="M13 6l6 6-6 6"/></svg>`,
  close: `<svg aria-hidden="true" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor"
    stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>`,
};

/* chips per screen: what is worth asking where you are standing */
const J_CHIPS = {
  waiting: ["What's waiting for me?", "Who are we waiting on, and since when?", "Summarise this week"],
  process: ["Why this recommendation?", "Draft reminder", "What do we owe this vendor?"],
  dashboard: ["Summarise this week", "How does the fraud score work?", "Biggest invoices this month"],
  payments: ["What's due this week?", "How much TDS do we deposit, and by when?", "Which MSME invoices are due soon?"],
  records: ["What's the price tolerance?", "What is a 3-way match?", "What is the MSME 45-day rule?"],
};

function jLocalGet(k) { try { return localStorage.getItem("invoice-desk:jeen:" + k); } catch (_) { return null; } }
function jLocalSet(k, v) { try { localStorage.setItem("invoice-desk:jeen:" + k, v); } catch (_) {} }

/* which screen is on, and what the user is looking at: Jeen answers "why?" against this */
function jCurrentView() {
  const b = document.querySelector('.tabs button[aria-selected="true"]');
  return (b && b.dataset.view) || "waiting";
}
function jContext() {
  const view = jCurrentView();
  let run_id = null;
  if (view === "process") run_id = (typeof state !== "undefined" && state.runId) || null;
  else if (view === "waiting") run_id = (typeof W !== "undefined" && W.sel) || null;
  return { view, run_id, vendor_id: null };
}

/* ------------------------------------------------------------------ build the panel */
function jMount() {
  if ($("#jeen-panel")) return;
  const launcher = document.createElement("button");
  launcher.type = "button";
  launcher.id = "jeen-launch";
  launcher.className = "jeen-launch";
  launcher.setAttribute("aria-label", "Ask Jeen, the AP assistant");
  launcher.innerHTML = `${J_ICON.jeen}<span>Ask Jeen</span>`;

  const panel = document.createElement("aside");
  panel.id = "jeen-panel";
  panel.className = "jeen-panel";
  panel.setAttribute("aria-label", "Jeen, the AP assistant");
  panel.hidden = true;
  panel.innerHTML = `
    <header class="jeen-head">
      <div class="jeen-head-l">
        <span class="jeen-mark" aria-hidden="true">${J_ICON.jeen}</span>
        <div><strong>Jeen</strong><span class="jeen-sub">AP assistant</span></div>
      </div>
      <div class="jeen-head-r">
        <span class="jeen-badge" id="jeen-mode" title="How Jeen is answering">…</span>
        <button type="button" class="jeen-x" id="jeen-close" aria-label="Close Jeen">${J_ICON.close}</button>
      </div>
    </header>
    <div class="jeen-log" id="jeen-log" aria-live="polite"></div>
    <div class="jeen-chips" id="jeen-chips"></div>
    <form class="jeen-form" id="jeen-form">
      <label class="jeen-sr" for="jeen-input">Ask Jeen a question</label>
      <textarea id="jeen-input" rows="1" placeholder="Ask about an invoice, a vendor, a PO…"
        autocomplete="off"></textarea>
      <button type="submit" class="jeen-send" id="jeen-send" aria-label="Ask">${J_ICON.send}</button>
    </form>
    <p class="jeen-foot">Jeen explains and prepares. Only you can approve, reject or pay.</p>`;

  document.body.append(launcher, panel);

  launcher.addEventListener("click", () => jToggle(true));
  $("#jeen-close").addEventListener("click", () => jToggle(false));
  $("#jeen-form").addEventListener("submit", (e) => { e.preventDefault(); jAsk($("#jeen-input").value); });
  const ta = $("#jeen-input");
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); jAsk(ta.value); }
  });
  ta.addEventListener("input", () => { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 120) + "px"; });
  jMode(jLocalGet("mode"));
  jHealth();
}

async function jHealth() {
  try {
    const h = await api("/api/health");
    jMode(h.llm ? "llm" : "rules");
  } catch (_) { /* the badge just stays as it was */ }
}

function jMode(mode) {
  JEEN.mode = mode || JEEN.mode;
  const el = $("#jeen-mode");
  if (!el || !JEEN.mode) return;
  const ai = JEEN.mode === "llm";
  el.textContent = ai ? "AI" : "Offline mode";
  el.classList.toggle("is-offline", !ai);
  el.title = ai ? "Answering with the language model, using Jeen's tools"
    : "No API key: answering from the rules router, using the same tools";
  jLocalSet("mode", JEEN.mode);
}

function jToggle(open) {
  JEEN.open = open === undefined ? !JEEN.open : open;
  const p = $("#jeen-panel");
  p.hidden = !JEEN.open;
  document.body.classList.toggle("jeen-is-open", JEEN.open);
  $("#jeen-launch").setAttribute("aria-expanded", String(JEEN.open));
  if (!JEEN.open) return;
  if (!JEEN.turns.length) {
    JEEN.turns.push({
      role: "jeen",
      text: "Hi, I'm Jeen. Ask me anything about your invoices, vendors or payments.",
      sources: [], actions: [], tools: [],
    });
  }
  jRender();
  setTimeout(() => $("#jeen-input").focus(), 30);
}

/* ------------------------------------------------------------------ asking */
function jThinking(q) {
  // what Jeen is about to go and look up, named from the question so the wait is legible
  const m = String(q).match(/\b(sundaram|kaveri|brightline|nimbus|apex|rapid ?print|murugan|ganesh)\b/i);
  if (m) return `Jeen is looking up ${m[1].replace(/\b\w/g, (c) => c.toUpperCase())}…`;
  const po = String(q).match(/\bPO[-\s]?\d{4}[-\s]?\d{3,5}\b/i);
  if (po) return `Jeen is checking ${po[0].toUpperCase()}…`;
  if (/due|payment|tds/i.test(q)) return "Jeen is checking the payment run…";
  if (/waiting|queue|my desk/i.test(q)) return "Jeen is reading your queue…";
  if (/toleran|match|irn|msme|gstr|fraud|ocr/i.test(q)) return "Jeen is reading the rule book…";
  if (/draft|remind|email/i.test(q)) return "Jeen is drafting it…";
  return "Jeen is looking it up…";
}

async function jAsk(text) {
  const q = String(text || "").trim();
  if (!q || JEEN.busy) return;
  $("#jeen-input").value = "";
  $("#jeen-input").style.height = "auto";
  JEEN.turns.push({ role: "you", text: q });
  JEEN.messages.push({ role: "user", content: q });
  JEEN.busy = true;
  JEEN.pending = jThinking(q);
  jRender();
  try {
    const r = await api("/api/jeen/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: JEEN.messages.slice(-10), context: jContext() }),
    });
    JEEN.turns.push({
      role: "jeen", text: r.answer || "(no answer)", sources: r.sources || [],
      actions: r.actions || [], tools: (r.tools_used || []).map((t) => t.tool), mode: r.mode,
    });
    JEEN.messages.push({ role: "assistant", content: r.answer || "" });
    jMode(r.mode);
  } catch (e) {
    JEEN.turns.push({ role: "jeen", text: `I couldn't answer that: ${e.message}`, sources: [], actions: [], tools: [] });
  }
  JEEN.busy = false;
  JEEN.pending = null;
  jRender();
}

/* ------------------------------------------------------------------ rendering */
const jText = (s) => esc(s).replace(/\n/g, "<br>");

function jRender() {
  const log = $("#jeen-log");
  if (!log) return;
  log.innerHTML = JEEN.turns.map((t, i) => {
    if (t.role === "you") return `<div class="jeen-msg you"><div class="jeen-b">${jText(t.text)}</div></div>`;
    const src = (t.sources || []).length
      ? `<div class="jeen-srcs">${t.sources.map((s, k) =>
        `<button type="button" class="jeen-src" data-turn="${i}" data-src="${k}"
          title="Open ${esc(s.kind)}">${esc(s.label)}</button>`).join("")}</div>` : "";
    const act = (t.actions || []).length
      ? `<div class="jeen-acts">${t.actions.map((a, k) =>
        `<button type="button" class="jeen-act" data-turn="${i}" data-act="${k}">${esc(a.label)}</button>`).join("")}</div>` : "";
    const tools = (t.tools || []).length
      ? `<div class="jeen-tools">Looked up: ${esc(t.tools.join(", "))}</div>` : "";
    return `<div class="jeen-msg jeen"><span class="jeen-av" aria-hidden="true">${J_ICON.jeen}</span>
      <div><div class="jeen-b">${jText(t.text)}</div>${src}${act}${tools}</div></div>`;
  }).join("");
  if (JEEN.pending) {
    log.insertAdjacentHTML("beforeend",
      `<div class="jeen-msg jeen"><span class="jeen-av" aria-hidden="true">${J_ICON.jeen}</span>
        <div class="jeen-b jeen-wait"><span class="jeen-dots"><i></i><i></i><i></i></span>${esc(JEEN.pending)}</div></div>`);
  }
  log.querySelectorAll(".jeen-src").forEach((b) => b.addEventListener("click", () =>
    jOpenSource(JEEN.turns[+b.dataset.turn].sources[+b.dataset.src])));
  log.querySelectorAll(".jeen-act").forEach((b) => b.addEventListener("click", () =>
    jDoAction(JEEN.turns[+b.dataset.turn].actions[+b.dataset.act], b)));
  log.scrollTop = log.scrollHeight;
  jChips();
  $("#jeen-send").disabled = JEEN.busy;
}

function jChips() {
  const el = $("#jeen-chips");
  if (!el) return;
  const list = J_CHIPS[jCurrentView()] || J_CHIPS.waiting;
  el.innerHTML = list.map((q) => `<button type="button" class="jeen-chip">${esc(q)}</button>`).join("");
  el.querySelectorAll(".jeen-chip").forEach((b, i) => b.addEventListener("click", () => jAsk(list[i])));
}

/* ------------------------------------------------------------------ source chips */
function jOpenSource(s) {
  if (!s) return;
  if (s.kind === "invoice") {
    if (typeof state !== "undefined") { state.open = new Set(["decision"]); state.lastStatus = {}; }
    show("process");
    openRun(s.id);
  } else if (s.kind === "po" || s.kind === "vendor" || s.kind === "rule") {
    show("records");
    toast(s.kind === "rule" ? "The rules are at the top of Records." : `${s.label} is in Records.`);
  } else if (s.kind === "payment") {
    show("payments");
  } else {
    show("dashboard");
  }
}

/* ------------------------------------------------------------------ action buttons */
async function jDoAction(a, btn) {
  if (!a) return;
  if (a.type === "open") {
    show(a.payload.view);
    if (a.payload.view === "process" && a.payload.id) openRun(a.payload.id);
    return;
  }
  if (a.type === "prepare_decision") {
    show("waiting");
    const want = a.payload.run_id;
    for (let i = 0; i < 20; i++) {              // the queue reloads when the tab opens
      if (typeof W !== "undefined" && W.items.some((x) => x.id === want)) break;
      await new Promise((ok) => setTimeout(ok, 150));
    }
    if (typeof W === "undefined" || !W.items.some((x) => x.id === want)) {
      toast("That invoice is no longer waiting for a decision.");
      return;
    }
    W.sel = want;
    renderList();
    renderDetail();
    const note = $("#w-note");
    if (note && a.payload.note) note.value = a.payload.note;
    $(".w-act")?.scrollIntoView({ block: "center", behavior: "smooth" });
    toast(`Form ready. You still click ${a.payload.action === "approve" ? "Approve" : "Reject"}.`);
    return;
  }
  if (a.type === "send_email") {
    btn.disabled = true;
    btn.textContent = "Sending…";
    try {
      const r = await api("/api/jeen/send", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(a.payload),
      });
      btn.textContent = `Sent to ${r.to}`;
      toast(r.status === "sent" ? `Sent to ${r.to}` : `Logged for ${r.to} (no webhook configured)`);
    } catch (e) {
      btn.disabled = false;
      btn.textContent = a.label;
      toast(e.message);
    }
  }
}

/* ------------------------------------------------------------------ keys + boot */
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); jToggle(); return; }
  if (e.key === "Escape" && JEEN.open) { e.preventDefault(); jToggle(false); }
});
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => {
  if (JEEN.open) jChips();   // the suggestions follow the screen
}));

jMount();
