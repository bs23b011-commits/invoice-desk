/* The invoice's discussion: one timeline of what happened, plus comments with @mentions.
   @jeen answers here with sources; @stores, @procurement, @vendor… get an email and their reply comes back here.
   Reuses jText / jOpenSource / jDoAction / J_ICON from jeen.js. */
"use strict";

const DISC = { run: null, handles: [], drafts: {}, busy: false };
const DISC_ICON = {
  agent: `<svg aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="8" width="14" height="11" rx="3"/><path d="M12 4v4"/></svg>`,
  mail: `<svg aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/></svg>`,
  dot: `<svg aria-hidden="true" width="10" height="10" viewBox="0 0 10 10"><circle cx="5" cy="5" r="4" fill="currentColor"/></svg>`,
};
const dTime = (iso) => { try { return fmtTime(iso); } catch (_) { return ""; } };
const dHtml = (t) => (typeof jText === "function" ? jText(t) : esc(t));
const withMentions = (t) => esc(t).replace(/(^|[^\w.])@([A-Za-z]+)/g, (m, pre, h) => `${pre}<span class="disc-at">@${h}</span>`);

async function loadDiscussion(runId, keepScroll) {
  const box = document.querySelector(`#w-disc[data-run="${runId}"] .disc-body`);
  if (!box) return;
  let d;
  try { d = await api(`/api/runs/${runId}/discussion`); } catch (e) { box.innerHTML = `<p class="muted small">${esc(e.message)}</p>`; return; }
  DISC.run = runId; DISC.handles = d.handles; DISC.items = d.items;
  const it = (typeof W !== "undefined") && W.items.find((i) => i.id === runId);
  if (it) it.comments = d.items.filter((x) => ["comment", "note", "jeen"].includes(x.kind)).length;  // keep polling quiet
  const pinned = `<div class="disc-pin"><span class="disc-av jeen" aria-hidden="true">${typeof J_ICON !== "undefined" ? J_ICON.jeen : ""}</span>
    <div><div class="disc-h">Jeen · summary</div><div>${dHtml(d.pinned.text)}</div></div></div>`;
  const items = d.items.map((it, i) => discItem(it, i)).join("");
  box.innerHTML = `${pinned}<div class="disc-list" id="disc-list">${items}</div>
    <div class="disc-compose">
      <label class="disc-label" for="disc-text">Comment, or @mention someone</label>
      <div class="disc-input-wrap">
        <textarea id="disc-text" rows="2" placeholder="@Jeen why is this on hold?  ·  @Stores did the rest arrive?"></textarea>
        <div class="disc-suggest" id="disc-suggest" role="listbox" hidden></div>
      </div>
      <div class="disc-chips">${["jeen", "stores", "procurement", "vendor", "ap"].map((h) =>
        `<button type="button" class="disc-chip" data-h="${h}">@${h === "jeen" ? "Jeen" : h[0].toUpperCase() + h.slice(1)}</button>`).join("")}
        <button type="button" class="w-btn ghost disc-post" id="disc-post">Post</button></div>
      <div class="muted small">Ctrl+Enter to post. @Jeen answers here; a team gets an email and its reply comes back here.</div>
    </div>`;
  const list = $("#disc-list");
  list.scrollTop = keepScroll ? list.scrollTop : list.scrollHeight;
  bindDiscussion(runId);
}

function discItem(it, i) {
  const when = it.at ? `<span class="disc-t">${esc(dTime(it.at))}</span>` : "";
  if (it.kind === "event" || it.kind === "decision") {
    return `<div class="disc-ev ${it.kind}">${DISC_ICON.dot}<span><b>${esc(it.author || "")}</b> ${esc(it.text)}</span>${when}</div>`;
  }
  if (it.kind === "note") return `<div class="disc-ev note ${it.blocked ? "blocked" : ""}">${DISC_ICON.mail}<span>${esc(it.text)}</span>${when}</div>`;
  if (it.kind === "email_out" || it.kind === "email_in") {
    const out = it.kind === "email_out";
    return `<div class="disc-msg email ${out ? "out" : "in"}">
      <div class="disc-h">${out ? DISC_ICON.agent + " Agent → " + esc(it.to || "") : DISC_ICON.mail + " " + esc(it.author) + " replied"} ${when}</div>
      <details><summary>${esc(it.subject || "(email)")}</summary><div class="disc-body-txt">${esc(it.text)}</div></details>
      ${it.understood ? `<div class="disc-u">Agent understood: <b>${esc(it.understood.replace(/_/g, " "))}</b>${it.outcome ? `. ${esc(it.outcome)}` : ""}</div>` : ""}
    </div>`;
  }
  if (it.kind === "jeen") {
    const src = (it.sources || []).length ? `<div class="jeen-srcs">${it.sources.map((s, k) =>
      `<button type="button" class="jeen-src" data-i="${i}" data-src="${k}">${esc(s.label)}</button>`).join("")}</div>` : "";
    const act = (it.actions || []).length ? `<div class="jeen-acts">${it.actions.map((a, k) =>
      `<button type="button" class="jeen-act" data-i="${i}" data-act="${k}">${esc(a.label)}</button>`).join("")}</div>` : "";
    return `<div class="disc-msg jeen"><span class="disc-av jeen" aria-hidden="true">${typeof J_ICON !== "undefined" ? J_ICON.jeen : ""}</span>
      <div><div class="disc-h">Jeen ${when}</div><div>${dHtml(it.text)}</div>${src}${act}</div></div>`;
  }
  return `<div class="disc-msg person"><span class="disc-av" aria-hidden="true">${esc(initials(it.author))}</span>
    <div><div class="disc-h">${esc(it.author)} ${when}</div><div class="disc-body-txt">${withMentions(it.text)}</div></div></div>`;
}

function bindDiscussion(runId) {
  const ta = $("#disc-text"), sug = $("#disc-suggest");
  ta.value = DISC.drafts[runId] || "";
  const insert = (h) => {
    const v = ta.value, at = v.slice(0, ta.selectionStart).search(/@\w*$/);
    const word = "@" + (h === "jeen" ? "Jeen" : h[0].toUpperCase() + h.slice(1)) + " ";
    if (at >= 0) ta.value = v.slice(0, at) + word + v.slice(ta.selectionStart);
    else ta.value = (v && !v.endsWith(" ") ? v + " " : v) + word;
    DISC.drafts[runId] = ta.value; sug.hidden = true; ta.focus();
  };
  ta.addEventListener("input", () => {
    DISC.drafts[runId] = ta.value;
    const m = ta.value.slice(0, ta.selectionStart).match(/@(\w*)$/);
    if (!m) { sug.hidden = true; return; }
    const hits = DISC.handles.filter((h) => h.handle.startsWith(m[1].toLowerCase()));
    sug.innerHTML = hits.map((h) => `<button type="button" role="option" data-h="${h.handle}"><b>@${esc(h.handle)}</b> ${esc(h.name)}<span>${esc(h.note)}</span></button>`).join("");
    sug.hidden = !hits.length;
    sug.querySelectorAll("button").forEach((b) => b.addEventListener("mousedown", (e) => { e.preventDefault(); insert(b.dataset.h); }));
  });
  ta.addEventListener("blur", () => setTimeout(() => (sug.hidden = true), 150));
  ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); postComment(runId); } });
  document.querySelectorAll(".disc-chip").forEach((b) => b.addEventListener("click", () => insert(b.dataset.h)));
  $("#disc-post").addEventListener("click", () => postComment(runId));
  const list = $("#disc-list");
  list.querySelectorAll(".jeen-src").forEach((b) => b.addEventListener("click", () =>
    jOpenSource(DISC.items[+b.dataset.i].sources[+b.dataset.src])));
  list.querySelectorAll(".jeen-act").forEach((b) => b.addEventListener("click", () =>
    jDoAction(DISC.items[+b.dataset.i].actions[+b.dataset.act], b)));
}

async function postComment(runId) {
  const ta = $("#disc-text");
  const text = (ta.value || "").trim();
  if (!text || DISC.busy) return;
  DISC.busy = true;
  W.busy = true;
  $("#disc-post").disabled = true;
  const asksJeen = /@jeen\b/i.test(text);
  $("#disc-list").insertAdjacentHTML("beforeend", discItem({ kind: "comment", author: localGet("who") || "AP manager", text }, -1)
    + (asksJeen ? `<div class="disc-msg jeen"><span class="disc-av jeen" aria-hidden="true">${typeof J_ICON !== "undefined" ? J_ICON.jeen : ""}</span>
      <div class="jeen-b jeen-wait"><span class="jeen-dots"><i></i><i></i><i></i></span>Jeen is looking this up…</div></div>` : ""));
  $("#disc-list").scrollTop = $("#disc-list").scrollHeight;
  const fd = new FormData(); fd.append("text", text); fd.append("author", localGet("who") || "AP manager");
  try {
    await api(`/api/runs/${runId}/comments`, { method: "POST", body: fd });
    DISC.drafts[runId] = "";
  } catch (e) { toast(e.message); }
  DISC.busy = false;
  W.busy = false;
  await loadDiscussion(runId);
}
