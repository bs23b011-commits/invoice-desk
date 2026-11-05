/* Bring your own data: vendors, purchase orders and goods receipts from CSV or Excel. */
"use strict";

const MASTER_KINDS = [
  { kind: "vendors", title: "Vendors", what: "name, GSTIN, bank and account number, MSME, payment terms" },
  { kind: "pos", title: "Purchase orders", what: "one row per PO line: PO number, vendor GSTIN, item, qty, rate, HSN" },
  { kind: "receipts", title: "Goods receipts", what: "what arrived: PO number, item, qty, date" },
];

async function loadMaster() {
  let m;
  try { m = await api("/api/master"); } catch (e) { return; }
  const count = { vendors: m.vendors, pos: m.pos, receipts: m.receipts };
  const any = m.imported.vendors + m.imported.pos + m.imported.receipts > 0;
  $("#master").innerHTML = `<div class="byod-grid">${MASTER_KINDS.map((k) => `
      <div class="byod-card">
        <div class="byod-h"><strong>${k.title}</strong><span class="mono">${count[k.kind]}</span></div>
        <p class="muted small">${k.what}${m.imported[k.kind] ? ` · <b>${m.imported[k.kind]} yours</b>` : ""}</p>
        <div class="byod-btns">
          <a class="btn ghost small" href="/api/master/template/${k.kind}.csv" download>Template</a>
          <label class="btn small">Upload CSV / Excel<input type="file" accept=".csv,.xlsx,text/csv" data-kind="${k.kind}" hidden></label>
        </div>
        <div class="byod-result" id="byod-${k.kind}" aria-live="polite"></div>
      </div>`).join("")}</div>
    <p class="muted small byod-order">Upload in this order: vendors, then purchase orders, then goods receipts.
      ${any ? `<button type="button" class="linkish" id="byod-clear">Remove my imported data</button>` : ""}</p>`;
  document.querySelectorAll("#master input[type=file]").forEach((inp) => inp.addEventListener("change", async () => {
    const f = inp.files[0]; if (!f) return;
    const out = $("#byod-" + inp.dataset.kind);
    out.textContent = "Importing…";
    const fd = new FormData(); fd.append("kind", inp.dataset.kind); fd.append("file", f);
    try {
      const r = await api("/api/master/import", { method: "POST", body: fd });
      out.innerHTML = `<span class="ok-text">${r.added} added${r.updated ? `, ${r.updated} updated` : ""}</span>` +
        (r.errors.length ? `<ul class="byod-errors">${r.errors.slice(0, 8).map((e) => `<li>Row ${e.row}: ${esc(e.error)}</li>`).join("")}
          ${r.errors.length > 8 ? `<li>…and ${r.errors.length - 8} more</li>` : ""}</ul>` : "");
      toast(`${r.added + r.updated} ${inp.dataset.kind} imported${r.errors.length ? `, ${r.errors.length} rows skipped` : ""}`);
      setTimeout(() => { loadRecords(); }, 900);
    } catch (e) { out.innerHTML = `<span class="bad-text">${esc(e.message)}</span>`; }
    inp.value = "";
  }));
  $("#byod-clear")?.addEventListener("click", async () => {
    await api("/api/master/clear", { method: "POST" });
    toast("Your imported vendors, POs and receipts were removed");
    loadRecords();
  });
}
