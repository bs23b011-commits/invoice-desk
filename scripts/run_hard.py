"""Score the agent on the HARD set (samples_hard/, built by scripts/make_hard.py).

A case passes only if
  - the decision is one a careful AP clerk would accept (expect),
  - the reason it gives is the right one (codes_any: at least one of these finding codes),
  - it reads the right amount (total), never raises codes it shouldn't (codes_none),
  - and nothing that isn't a bill is ever recommended for payment (no_payment).
Run:  python scripts/run_hard.py [-v] [name-filter]
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.update(STAGE_DELAY="0", RUNTIME_DIR=tempfile.mkdtemp(), DEMO_TODAY="2026-11-04", APPROVAL_MODE="human")
for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
    os.environ.pop(k, None)   # score the deterministic brain, not an LLM
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import files, pipeline, store  # noqa: E402
from app import main as appmain  # noqa: E402

D = ROOT / "samples_hard"


def codes_of(run: dict) -> set[str]:
    out = set()
    for s in run.get("stages") or []:
        for f in s.get("findings") or []:
            if f.get("code"):
                out.add(f["code"])
    for r in (run.get("result") or {}).get("reasons") or []:
        if isinstance(r, dict) and r.get("code"):
            out.add(r["code"])
    return out


def run_one(name: str) -> dict:
    """Same path as an upload: normalise, split, then check (first invoice of the file is scored)."""
    data = (D / name).read_bytes()
    try:
        name, data, _ = files.normalise(name, data)
    except files.Unreadable as e:
        run = pipeline.unreadable_run(name, str(e))
        return dict(run, state="unreadable", result=dict(decision="UNREADABLE", headline=str(e)))
    runs, t = appmain._start(name, data, None, None, "hard set")
    t.join()
    return store.get_run(runs[0]["id"]) or runs[0]


def score(case: dict, run: dict) -> tuple[bool, list[str]]:
    res = run.get("result") or {}
    dec = res.get("decision") or ("UNREADABLE" if run.get("state") in ("unreadable",) or run.get("unreadable")
                                  else "ERROR")
    if run.get("state") == "error":
        dec = "ERROR"
    why = []
    if dec not in case["expect"]:
        why.append(f"decision {dec}, wanted {'/'.join(case['expect'])}")
    cs = codes_of(run)
    if case.get("codes_any") and not cs & set(case["codes_any"]):
        why.append(f"no {' or '.join(case['codes_any'])} finding")
    for c in case.get("codes_also") or []:
        if c not in cs:
            why.append(f"missing {c}")
    bad = cs & set(case.get("codes_none") or [])
    if bad:
        why.append(f"wrongly raised {', '.join(sorted(bad))}")
    if case.get("total") is not None:
        t = run.get("total")
        if t is None or abs(float(t) - case["total"]) > 1:
            why.append(f"read total {t}, actual {case['total']}")
    if case.get("no_payment") and dec == "APPROVE" and (run.get("total") or 0) > 0:
        why.append("recommended paying a document that isn't a bill")
    if case.get("not_unreadable") and dec in ("UNREADABLE", "ERROR"):
        why.append("gave up on a readable page")
    if case.get("mention"):
        said = [f.get("text", "") for st in run.get("stages") or [] for f in st.get("findings") or []]
        said += [res.get("headline") or "", res.get("next_action") or ""]
        blob = " ".join(said).lower()
        if case["mention"].lower() not in blob:
            why.append(f"never mentions '{case['mention']}'")
    return not why, why


def main() -> None:
    verbose = "-v" in sys.argv
    filt = [a for a in sys.argv[1:] if not a.startswith("-")]
    cases = json.loads((D / "EXPECTED.json").read_text(encoding="utf-8"))
    ok = 0
    seq = None
    rows = []
    for c in cases:
        if filt and not any(f in c["file"] for f in filt):
            continue
        if c.get("sequence") is None or c.get("sequence") != seq:
            store.init(reset=True)
        seq = c.get("sequence")
        run = run_one(c["file"])
        good, why = score(c, run)
        ok += good
        res = run.get("result") or {}
        dec = res.get("decision") or run.get("state")
        rows.append(dict(file=c["file"], ok=good, decision=dec, why=why, codes=sorted(codes_of(run)),
                         total=run.get("total"), headline=res.get("headline") or run.get("error")))
        print(f"{'PASS' if good else 'FAIL'}  {c['file']:34} {str(dec):10} {'; '.join(why)}")
        if verbose or not good:
            print(f"      codes: {', '.join(sorted(codes_of(run))) or '-'} | total {run.get('total')}")
            print(f"      says:  {(res.get('headline') or run.get('error') or '')[:220]}")
    n = len(rows)
    print(f"\n{ok}/{n} hard cases handled the way a careful AP clerk would")
    _write_doc(cases, rows, ok, n)
    (ROOT / "data" / "hard_report.json").write_text(json.dumps(dict(passed=ok, total=n, rows=rows), indent=1,
                                                               ensure_ascii=False), encoding="utf-8")


def _write_doc(cases: list[dict], rows: list[dict], ok: int, n: int) -> None:
    by = {r["file"]: r for r in rows}
    out = ["# The hard set: real-world failures of AP tools, and what Invoice Desk does",
           "",
           "Built from public complaints and audit write-ups about AP automation (Stampli, BILL and Ramp reviews and",
           "forums, OCR failure-mode write-ups, Indian GST/TDS audit findings). Each PDF in `samples_hard/` has one",
           "problem; the right answer is what a careful AP clerk would do. Regenerate with `python scripts/make_hard.py`,",
           "score with `python scripts/run_hard.py`.",
           "",
           f"**{ok}/{n} handled correctly.** A case passes only with the right decision *and* the right reason, the right",
           "amount read, and no payment recommended for anything that isn't a bill.",
           "",
           "| # | The problem | Where it comes from | Agent | Why that's right |",
           "|---|---|---|---|---|"]
    for c in cases:
        r = by.get(c["file"])
        if not r:
            continue
        mark = "" if r["ok"] else " ❌"
        out.append(f"| {c['file'].split('_')[0]} | {c['what']} | {c['source']} | **{r['decision']}**{mark} | {c['why']} |")
    (ROOT / "docs" / "HARD_CASES.md").write_text("\n".join(out) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
