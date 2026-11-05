"""Step 6c: measure the whole process on invoices with known answers.

  python scripts/evaluate.py              # 4 invoices per situation (40 total)
  python scripts/evaluate.py --per-type 10

Generates fresh invoices (4 layouts, digital and fax-quality scans) for 10 situations, each with the correct
fields and the correct decision, runs every one through the full pipeline in an isolated workspace, and reports:

  decision accuracy      did it approve / hold / reject correctly
  false approvals        should NOT have been paid but was approved      <- the expensive error; must be 0
  silent errors          approved, but a key field was read wrong       <- the dangerous error; must be 0
  missed approvals       a good invoice sent to a person (costs time, not money)
  field accuracy         per field, per layout, digital vs scan
  checker sensitivity    inject single-digit errors into correct readings: how many does the arithmetic catch?

Writes data/eval_report.json (shown on the dashboard) and docs/EVALUATION.md.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import random
import shutil
import sys
import tempfile
import time
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = Path(tempfile.mkdtemp(prefix="invoice_eval_"))
DATA = WORK / "data"
DATA.mkdir()
os.environ.update(INVOICE_DESK_DATA=str(DATA), RUNTIME_DIR=str(WORK / "runtime"), STAGE_DELAY="0",
                  DEMO_TODAY="2026-11-04", N8N_WEBHOOK_URL="")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import make_data as M  # noqa: E402  (templates, vendors, scanner)

SITUATIONS = [
    # key, expected decision, acceptable alternatives, description
    ("clean", "APPROVE", (), "Correct invoice, explicit PO"),
    ("scan", "APPROVE", ("REVIEW",), "Correct invoice, fax-quality scan (REVIEW acceptable if unreadable)"),
    ("price_over", "REVIEW", (), "One line priced 3% above the PO"),
    ("qty_over", "REVIEW", (), "Bills more units than the PO allows"),
    ("no_po", "REVIEW", (), "PO number missing"),
    ("duplicate", "REJECT", (), "Earlier invoice re-sent with the number re-formatted"),
    ("bad_math", "REJECT", (), "Digital invoice whose total doesn't add up"),
    ("bank_change", "REVIEW", (), "Asks for payment to a new bank account"),
    ("blocked", "REJECT", (), "Vendor blocked in the master"),
    ("wrong_tax", "REVIEW", (), "Same-state vendor charges IGST"),
]
GOOD_VENDORS = ["V001", "V002", "V003", "V004", "V005", "V007"]
CATALOG = {}  # vendor -> list of (description, price, hsn)
for po in M.POS:
    for l in po["lines"]:
        CATALOG.setdefault(po["vendor_id"], []).append((l["description"], l["unit_price"], l["hsn"]))
TEMPLATES = dict(classic=M.tpl_classic, modern=M.tpl_modern, letter=M.tpl_letter, statement=M.tpl_statement)
FMT = {"classic": "%d-%b-%Y", "modern": "%B %d, %Y", "letter": "%d/%m/%Y", "statement": "%d %b %Y"}


def make_cases(per_type: int, seed: int):
    rnd = random.Random(seed)
    pos, grns, cases, made = [], [], [], []
    n = 0
    for key, expect, ok_alt, desc in SITUATIONS:
        for k in range(per_type):
            n += 1
            vid = "V006" if key == "blocked" else rnd.choice(GOOD_VENDORS)
            if key == "wrong_tax":
                vid = rnd.choice([v for v in GOOD_VENDORS if M.VBY[v]["state"] == "Tamil Nadu"])
            v = M.VBY[vid]
            # statement layout has unusual labels (the learning demo) - evaluate the 3 standard layouts here
            tpl = "classic" if key == "scan" else rnd.choice(["classic", "modern", "letter"])
            items = rnd.sample(CATALOG[vid], k=min(len(CATALOG[vid]), rnd.randint(1, 3)))
            lines = []
            for desc_, price, hsn in items:
                qty = rnd.choice([1, 2, 4, 5, 6, 10, 12, 20, 40, 50, 100, 200, 500])
                if price < 5:
                    qty *= 1000
                lines.append(dict(description=desc_, qty=qty, unit_price=price, hsn=hsn, unit="Nos"))
            po_no = f"PO-2027-{n:04d}"
            po_lines = [dict(description=l["description"], qty=l["qty"], unit_price=l["unit_price"],
                             amount=round(l["qty"] * l["unit_price"], 2), hsn=l["hsn"]) for l in lines]
            if key == "qty_over":
                lines[0] = dict(lines[0], qty=lines[0]["qty"] + max(1, lines[0]["qty"] // 2))
            if key == "price_over":
                lines[0] = dict(lines[0], unit_price=round(lines[0]["unit_price"] * 1.03, 2))
            pos.append(dict(po_number=po_no, vendor_id=vid, date="2026-09-15", status="open", lines=po_lines,
                            amount=round(sum(l["amount"] for l in po_lines), 2)))
            for l in po_lines:
                if not l["hsn"].startswith("99"):
                    grns.append(dict(grn=f"GRN-E-{len(grns) + 1:05d}", po_number=po_no, description=l["description"],
                                     qty=l["qty"] * 2, date="2026-10-01"))
            d = date(2026, 10, 1) + timedelta(days=rnd.randint(0, 28))
            while d.weekday() == 6:
                d += timedelta(days=1)
            number = f"{''.join(ch for ch in v['name'] if ch.isupper())[:3]}/{rnd.randint(1000, 9999)}"
            tax_override = None
            if key == "wrong_tax":
                sub = round(sum(l["qty"] * l["unit_price"] for l in lines), 2)
                tax_override = [("IGST @ 18%", round(sub * 0.18, 2))]
            bank_text = None
            if key == "bank_change":
                bank_text = "Our bank details have changed. Please pay to our NEW account: Yes Bank ****" + \
                            f"{rnd.randint(1000, 9999)}   |   Payment terms: {v['terms_days']} days"
            inv = M.build_invoice(vid, number, d.strftime(FMT[tpl]), "", None if key == "no_po" else po_no, lines,
                                  tax_override=tax_override, bank_text=bank_text)
            if key == "bad_math":
                inv["total"] = round(inv["total"] + rnd.choice([100, 1000, 90]), 2)
            truth = dict(invoice_number=number, invoice_date=d.isoformat(), po_number=po_no, vendor_id=vid,
                         subtotal=inv["subtotal"], total=inv["total"], lines=len(lines))
            case = dict(id=f"{n:03d}_{key}", situation=key, expect=expect, ok=(expect,) + tuple(ok_alt), template=tpl,
                        scanned=key == "scan", truth=truth, inv=inv, vid=vid)
            if key == "duplicate":
                orig = rnd.choice([c for c in made if c["situation"] == "clean"])
                case = dict(copy.deepcopy(orig), id=f"{n:03d}_{key}", situation=key, expect=expect, ok=(expect,))
                case["inv"]["number"] = case["inv"]["number"].replace("/", "-")  # same number, re-formatted
                case["truth"]["invoice_number"] = case["inv"]["number"]
                case["template"] = "letter" if orig["template"] != "letter" else "classic"
            cases.append(case)
            made.append(case)
    return cases, pos, grns


def render(case, folder: Path):
    v = M.VBY[case["vid"]]
    path = folder / f"{case['id']}.pdf"
    inv = case["inv"]
    if inv.get("irn"):  # the IRN belongs to the number actually printed
        inv["irn"] = M.irn_for(v["gstin"], case["truth"]["invoice_date"], inv["number"])
    show_po = inv.get("po") is not None
    if case["scanned"]:
        tmp = folder / f"_{case['id']}.pdf"
        TEMPLATES[case["template"]](tmp, v, inv, show_po=show_po)
        M.scan_it(tmp, path, seed=int(case["id"][:3]))
        tmp.unlink()
    else:
        TEMPLATES[case["template"]](path, v, inv, show_po=show_po)
    return path


def mutation_test(readings, rnd):
    """Inject one realistic error (change one digit) into a correct reading; does the arithmetic notice?"""
    from app import checks
    tried = caught = paise = 0
    missed = []
    for inv in readings:
        cells = [("lines", i, k) for i in range(len(inv["lines"])) for k in ("qty", "unit_price", "amount")]
        cells += [("taxes", j, "amount") for j in range(len(inv["taxes"]))] + [("total",)]
        if not inv.get("tax_inclusive") and inv.get("subtotal") is not None:
            cells.append(("subtotal",))
        for _ in range(25):
            cell = rnd.choice(cells)
            m = copy.deepcopy(inv)
            val = checks._get(m, cell)
            s = f"{val:.2f}".replace(".", "")
            pos = rnd.randrange(len(s))
            new = s[:pos] + rnd.choice([d for d in "0123456789" if d != s[pos]]) + s[pos + 1:]
            newv = float(new[:-2] + "." + new[-2:])
            if newv == val or newv <= 0:
                continue
            checks._set(m, cell, newv)
            tried += 1
            ok = all(i["ok"] for i in checks.identities(m))
            if not ok:
                caught += 1
            elif abs(newv - val) <= 1.0 + 1e-9:  # inside the ₹1 round-off tolerance by design
                paise += 1
            else:
                missed.append(dict(cell="/".join(map(str, cell)), was=val, became=newv))
    return dict(tried=tried, caught=caught, missed_within_rounding=paise,
                missed_other=tried - caught - paise,
                rate=round(caught / tried * 100, 1) if tried else None,
                rate_rupee_or_more=round(caught / max(1, tried - paise) * 100, 1) if tried else None,
                missed_examples=missed[:8])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-type", type=int, default=4)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    t0 = time.time()
    cases, pos, grns = make_cases(a.per_type, a.seed)
    for f in ("vendors.json", "buyer.json"):
        shutil.copy(ROOT / "data" / f, DATA / f)
    (DATA / "purchase_orders.json").write_text(json.dumps(pos))
    (DATA / "goods_receipts.json").write_text(json.dumps(grns))
    (DATA / "invoice_history.json").write_text("[]")
    (DATA / "gstr2b.json").write_text("[]")
    pdfs = WORK / "pdfs"
    pdfs.mkdir()
    for c in cases:
        c["path"] = render(c, pdfs)

    from app import pipeline, store
    store.init(reset=True)
    rows, readings = [], []
    for c in cases:
        pdf = c["path"].read_bytes()
        r = pipeline.new_run(c["path"].name, None)
        store.file_path(r["id"], "pdf").write_bytes(pdf)
        t = time.time()
        pipeline.execute(r, pdf)
        r = store.get_run(r["id"])
        res = r.get("result") or {}
        got = res.get("decision", "ERROR")
        inv = res.get("invoice") or {}
        tr = c["truth"]
        fields = dict(
            invoice_number=inv.get("invoice_number") == tr["invoice_number"],
            invoice_date=inv.get("invoice_date") == tr["invoice_date"],
            vendor=inv.get("vendor_id") == tr["vendor_id"],
            total=inv.get("total") is not None and abs(inv["total"] - tr["total"]) < 0.01,
            subtotal=inv.get("subtotal") is not None and abs(inv["subtotal"] - tr["subtotal"]) < 1.0,
            lines=len(inv.get("lines") or []) == tr["lines"],
        )
        if c["situation"] != "no_po":
            fields["po_number"] = inv.get("po_number") == tr["po_number"]
        if got == "APPROVE" and c["situation"] in ("clean", "scan"):
            readings.append(dict(lines=inv["lines"], taxes=inv["taxes"], subtotal=inv["subtotal"],
                                 total=inv["total"], tax_inclusive=False))
        rows.append(dict(id=c["id"], situation=c["situation"], template=c["template"], scanned=c["scanned"],
                         expect=c["expect"], got=got, correct=got in c["ok"], fields=fields,
                         false_approval=got == "APPROVE" and c["expect"] != "APPROVE",
                         silent_error=got == "APPROVE" and not all(fields.values()),
                         missed_approval=c["expect"] == "APPROVE" and got != "APPROVE",
                         repaired=inv.get("repaired"), seconds=round(time.time() - t, 2),
                         headline=res.get("headline")))
        print(f"{c['id']:18s} {c['template']:9s} expect {c['expect']:7s} got {got:7s} "
              f"{'ok' if rows[-1]['correct'] else 'WRONG'}  fields {sum(fields.values())}/{len(fields)}")

    # ---- summarise -------------------------------------------------------------------------------
    N = len(rows)
    by_sit = []
    for key, expect, _, desc in SITUATIONS:
        rs = [r for r in rows if r["situation"] == key]
        by_sit.append(dict(situation=key, description=desc, expected=expect, n=len(rs),
                           correct=sum(r["correct"] for r in rs), outcomes=dict(Counter(r["got"] for r in rs))))
    field_acc = {}
    for f in ("invoice_number", "invoice_date", "po_number", "vendor", "subtotal", "total", "lines"):
        vals = [r["fields"][f] for r in rows if f in r["fields"]]
        field_acc[f] = round(sum(vals) / len(vals) * 100, 1)
    by_layout = defaultdict(lambda: [0, 0])
    for r in rows:
        k = r["template"] + (" (scan)" if r["scanned"] else "")
        by_layout[k][0] += sum(r["fields"].values())
        by_layout[k][1] += len(r["fields"])
    report = dict(
        generated=date.today().isoformat(), invoices=N, per_type=a.per_type, seed=a.seed,
        decision_accuracy=round(sum(r["correct"] for r in rows) / N * 100, 1),
        false_approvals=sum(r["false_approval"] for r in rows),
        silent_errors=sum(r["silent_error"] for r in rows),
        missed_approvals=sum(r["missed_approval"] for r in rows),
        good_invoices=sum(1 for r in rows if r["expect"] == "APPROVE"),
        field_accuracy=field_acc,
        layout_field_accuracy={k: round(v[0] / v[1] * 100, 1) for k, v in sorted(by_layout.items())},
        situations=by_sit,
        checker=mutation_test(readings, random.Random(a.seed)),
        scans=dict(n=sum(r["scanned"] for r in rows), repaired=sum(1 for r in rows if r["scanned"] and r["repaired"]),
                   approved=sum(1 for r in rows if r["scanned"] and r["got"] == "APPROVE")),
        avg_seconds=round(sum(r["seconds"] for r in rows) / N, 2), total_seconds=round(time.time() - t0, 1),
        wrong=[r for r in rows if not r["correct"] or r["silent_error"]],
        held=[dict(id=r["id"], got=r["got"], why=r["headline"]) for r in rows if r["missed_approval"]])
    (ROOT / "data" / "eval_report.json").write_text(json.dumps(report, indent=2))
    write_markdown(report)
    print(f"\nDecision accuracy {report['decision_accuracy']}% on {N} invoices | false approvals "
          f"{report['false_approvals']} | silent errors {report['silent_errors']} | missed approvals "
          f"{report['missed_approvals']} | checker catches {report['checker']['rate_rupee_or_more']}% of errors > Rs.1")
    shutil.rmtree(WORK, ignore_errors=True)


def write_markdown(r):
    docs = ROOT / "docs"
    docs.mkdir(exist_ok=True)
    L = [f"# Evaluation report", "",
         f"{r['invoices']} generated invoices ({r['per_type']} per situation, seed {r['seed']}), 3 layouts, digital and "
         f"fax-quality scans, each with known correct fields and decision. Run with `python scripts/evaluate.py`.", "",
         "| Measure | Result | What it means |", "|---|---|---|",
         f"| Decision accuracy | **{r['decision_accuracy']}%** | approve / hold / reject matched the right answer |",
         f"| False approvals | **{r['false_approvals']}** | invoices that should not be paid but were approved |",
         f"| Silent errors | **{r['silent_errors']}** | approved with a wrongly read field |",
         f"| Missed approvals | {r['missed_approvals']} of {r['good_invoices']} | good invoices sent to a person anyway |",
         f"| Arithmetic check | catches **{r['checker']['rate_rupee_or_more']}%** of single-digit errors above ₹1 "
         f"({r['checker']['tried']} injected) | errors of ₹1 or less sit inside the round-off tolerance by design |",
         f"| Scans | {r['scans']['approved']} of {r['scans']['n']} approved, {r['scans']['repaired']} after an OCR repair | |",
         f"| Speed | {r['avg_seconds']} s per invoice | |", "",
         "## By situation", "", "| Situation | Expected | Correct | Outcomes |", "|---|---|---|---|"]
    for s in r["situations"]:
        L.append(f"| {s['description']} | {s['expected']} | {s['correct']}/{s['n']} | "
                 + ", ".join(f"{k} {v}" for k, v in s["outcomes"].items()) + " |")
    L += ["", "## Field accuracy", "", "| Field | Accuracy |", "|---|---|"]
    L += [f"| {k.replace('_', ' ')} | {v}% |" for k, v in r["field_accuracy"].items()]
    L += ["", "| Layout | Field accuracy |", "|---|---|"] + [f"| {k} | {v}% |" for k, v in r["layout_field_accuracy"].items()]
    if r["wrong"]:
        L += ["", "## Cases it got wrong", ""] + [f"- `{w['id']}` ({w['template']}): expected {w['expect']}, got "
                                                   f"{w['got']}. {w['headline'] or ''}" for w in r["wrong"]]
    (docs / "EVALUATION.md").write_text("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
