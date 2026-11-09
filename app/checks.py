"""Stage 4: the invoice must agree with itself before we compare it with anything else.

An invoice carries redundant information: qty x rate = amount, lines add up to the subtotal,
subtotal + tax = total, tax = subtotal x rate, CGST = SGST. If every identity holds, the numbers
we read are almost certainly the numbers on the paper - without needing a human or ground truth.

If identities fail on an OCR'd document, we try realistic OCR confusions (8<->6, 1<->4/7, a dropped
leading digit ...) on one number at a time, and accept a fix only if it makes EVERY identity hold
and it is the ONLY fix that does. Every repair is logged. On a digital PDF we never repair:
a failed identity there is the vendor's mistake, not ours.
"""
from __future__ import annotations

import copy
from datetime import date
from itertools import combinations

from .config import GST_RATES, GST_RATES_BEFORE_2025_09_22, GST2_START, ROUNDING
from .utils import fmt_inr

OCR_CONFUSIONS = {
    "0": "86", "1": "47", "2": "7", "3": "85", "4": "19", "5": "63", "6": "850", "7": "12",
    "8": "0365", "9": "40",
}


def _close(a, b, tol=ROUNDING):
    return a is not None and b is not None and abs(a - b) <= tol


def _adj(inv: dict, after: bool) -> float:
    return round(sum(a["amount"] or 0 for a in inv.get("adjustments") or [] if bool(a.get("after_subtotal")) == after), 2)


def effective_subtotal(inv: dict):
    """Pre-tax (taxable) amount. For tax-inclusive invoices it is derived: total - tax. Discounts or freight
    printed between the subtotal and the tax lines change the taxable value."""
    tax = sum(t["amount"] or 0 for t in inv["taxes"])
    if inv.get("tax_inclusive"):
        return round(inv["total"] - tax, 2) if inv.get("total") is not None else None
    if inv.get("subtotal") is not None:
        return round(inv["subtotal"] + _adj(inv, True), 2)
    if inv["lines"]:
        return round(sum(l["amount"] or 0 for l in inv["lines"]) + _adj(inv, False) + _adj(inv, True), 2)
    return None


def _full_rate(t: dict) -> float:
    return t["rate"] * (2 if t["type"] in ("CGST", "SGST", "UTGST") else 1)


def rate_groups(inv: dict) -> dict:
    """{full GST rate: [tax indexes]} - one invoice can carry goods at 18% and others at 5%."""
    g: dict = {}
    for j, t in enumerate(inv["taxes"]):
        if t.get("rate") is not None:
            g.setdefault(_full_rate(t), []).append(j)
    return g


def _subset_sum(amounts: list[float], target: float, tol: float = ROUNDING):
    """Indexes of lines that add up to target (small invoices only: up to 16 lines)."""
    n = len(amounts)
    if n > 16:
        return None
    for k in range(1, n + 1):
        for idx in combinations(range(n), k):
            if abs(sum(amounts[i] for i in idx) - target) <= tol:
                return list(idx)
    return None


def identities(inv: dict) -> list[dict]:
    out = []
    lines, taxes, total = inv["lines"], inv["taxes"], inv.get("total")
    inclusive = inv.get("tax_inclusive")

    for i, l in enumerate(lines):
        if None in (l.get("qty"), l.get("unit_price"), l.get("amount")):
            continue
        calc = round(l["qty"] * l["unit_price"], 2)
        out.append(dict(id=f"line{i+1}", label=f"Line {i+1}: qty × rate = amount",
                        expected=calc, found=l["amount"], ok=_close(calc, l["amount"]),
                        cells=[("lines", i, "qty"), ("lines", i, "unit_price"), ("lines", i, "amount")]))

    if lines:
        s = round(sum(l["amount"] or 0 for l in lines) + _adj(inv, False), 2)
        adj_cells = [("adjustments", k, "amount") for k, a in enumerate(inv.get("adjustments") or [])
                     if not a.get("after_subtotal")]
        if inclusive:
            out.append(dict(id="lines_total", label="Lines add up to the (tax-inclusive) total",
                            expected=s, found=total, ok=_close(s, total),
                            cells=[("lines", i, "amount") for i in range(len(lines))] + [("total",)]))
        elif inv.get("subtotal") is not None:
            out.append(dict(id="lines_subtotal", label="Lines add up to the subtotal" + (
                                " (after the discount / charges printed below them)" if adj_cells else ""),
                            expected=s, found=inv["subtotal"], ok=_close(s, inv["subtotal"]),
                            cells=[("lines", i, "amount") for i in range(len(lines))] + adj_cells + [("subtotal",)]))

    tax_sum = round(sum(t["amount"] or 0 for t in taxes), 2)
    if not inclusive and inv.get("subtotal") is not None and total is not None:
        calc = round(inv["subtotal"] + _adj(inv, True) + tax_sum, 2)
        out.append(dict(id="total", label="Subtotal + tax = total", expected=calc, found=total,
                        ok=_close(calc, total),
                        cells=[("subtotal",)] + [("taxes", j, "amount") for j in range(len(taxes))] + [("total",)]))

    base = effective_subtotal(inv)
    groups = rate_groups(inv)
    if len(groups) > 1 and base is not None and not inclusive:
        # several GST rates on one invoice: the taxable value splits into one part per rate, and each part must
        # be made of whole lines
        parts, used = {}, set()
        amounts = [l["amount"] or 0 for l in lines]
        for r, js in sorted(groups.items(), key=lambda kv: -kv[0]):
            pair = [taxes[j] for j in js]
            share = sum(t["amount"] or 0 for t in pair)
            parts[r] = round(share * 100 / r, 2) if r else 0.0
        out.append(dict(id="rate_split", label="Taxable value splits by GST rate: " + " + ".join(
                            f"{fmt_inr(v)} at {r:g}%" for r, v in parts.items()),
                        expected=base, found=round(sum(parts.values()), 2),
                        ok=abs(sum(parts.values()) - base) <= ROUNDING * len(parts) + 0.5,
                        cells=[("taxes", j, "amount") for j in range(len(taxes))] + [("subtotal",)]))
        for r, v in parts.items():
            free = [i for i in range(len(amounts)) if i not in used]
            pick = _subset_sum([amounts[i] for i in free], v, ROUNDING * 2)
            if pick is not None:
                used.update(free[i] for i in pick)
            out.append(dict(id=f"rate{r:g}", label=f"Lines taxed at {r:g}% add up to {fmt_inr(v)}",
                            expected=v, found=round(sum(amounts[free[i]] for i in pick), 2) if pick else None,
                            ok=pick is not None or not lines,
                            cells=[("taxes", j, "amount") for j in groups[r]]))
            js = groups[r]
            ts = [taxes[j] for j in js]
            if len(ts) == 2 and {t["type"] for t in ts} >= {"CGST", "SGST"}:
                out.append(dict(id=f"cgst_sgst{r:g}", label=f"CGST = SGST at {r:g}%", expected=ts[0]["amount"],
                                found=ts[1]["amount"], ok=_close(ts[0]["amount"], ts[1]["amount"]),
                                cells=[("taxes", js[0], "amount"), ("taxes", js[1], "amount")]))
        return out

    for j, t in enumerate(taxes):
        if t.get("rate") is None or base is None:
            continue
        if inclusive:
            calc = round(total * t["rate"] / (100 + t["rate"]), 2)
            cells = [("taxes", j, "amount"), ("total",)]
        else:
            calc = round(base * t["rate"] / 100, 2)
            cells = [("taxes", j, "amount"), ("subtotal",)]
        out.append(dict(id=f"tax{j}", label=f"{t['type']} = {t['rate']:g}% of taxable value",
                        expected=calc, found=t["amount"], ok=_close(calc, t["amount"]), cells=cells))

    by_type = {t["type"]: (j, t) for j, t in enumerate(taxes)}
    if "CGST" in by_type and "SGST" in by_type:
        (j1, c), (j2, s_) = by_type["CGST"], by_type["SGST"]
        out.append(dict(id="cgst_sgst", label="CGST = SGST (they are always equal halves)",
                        expected=c["amount"], found=s_["amount"], ok=_close(c["amount"], s_["amount"]),
                        cells=[("taxes", j1, "amount"), ("taxes", j2, "amount")]))
    return out


def rate_findings(inv: dict) -> list[dict]:
    """GST slabs changed on 22 Sep 2025 (GST 2.0): 12% and 28% were folded into 5%, 18% and 40% for almost
    everything. Vendors whose billing software wasn't updated still print 12%."""
    out, seen = [], set()
    try:
        d = date.fromisoformat(inv.get("invoice_date")) if inv.get("invoice_date") else None
    except ValueError:
        d = None
    new_regime = d is None or d >= GST2_START
    valid = GST_RATES if new_regime else GST_RATES_BEFORE_2025_09_22
    for t in inv["taxes"]:
        if t.get("rate") is None:
            continue
        r = _full_rate(t)
        if r in seen or r in valid:
            continue
        seen.add(r)
        if new_regime and r in GST_RATES_BEFORE_2025_09_22:
            out.append(dict(status="warn", text=f"GST charged at {r:g}% ({t['type']} {t['rate']:g}%): the {r:g}% slab "
                                                f"was removed on 22 Sep 2025 (GST 2.0) - most such goods are now 5% or "
                                                f"18%. The vendor's billing may still use the old rate; check the HSN "
                                                f"rate and ask for a corrected invoice"))
        else:
            out.append(dict(status="warn", text=f"{t['type']} rate {t['rate']:g}% is not a valid GST slab"))
    return out


# ------------------------------------------------------------------------------------------------
def _get(inv, path):
    cur = inv
    for p in path:
        cur = cur[p]
    return cur


def _set(inv, path, val):
    cur = inv
    for p in path[:-1]:
        cur = cur[p]
    cur[path[-1]] = val


def _variants(value: float):
    """Numbers OCR could plausibly have produced instead of the true one (single-character changes)."""
    s = f"{value:.2f}"
    whole, frac = s.split(".")
    digits = whole + frac
    seen = set()
    for i, ch in enumerate(digits):
        for alt in OCR_CONFUSIONS.get(ch, ""):
            seen.add(digits[:i] + alt + digits[i + 1:])
    for lead in "123456789":  # a dropped leading digit (e.g. '1,13,752' read as '13,752')
        seen.add(lead + digits)
    if len(whole) > 1:
        seen.add(whole[1:] + frac)  # a spurious extra leading digit
    for d in seen:
        if not d or d == digits:
            continue
        try:
            v = float(d[:-2] + "." + d[-2:])
        except ValueError:
            continue
        if v > 0:
            yield round(v, 2)


def try_repair(inv: dict):
    """Return (repaired_inv, repair_info) or (None, reason)."""
    ids = identities(inv)
    failing = [x for x in ids if not x["ok"]]
    if not failing:
        return None, "nothing to repair"
    suspect = []
    for x in failing:
        for c in x["cells"]:
            if c not in suspect:
                suspect.append(c)

    fixes = []
    for cell in suspect:
        try:
            original = _get(inv, cell)
        except (KeyError, IndexError, TypeError):
            continue
        if original is None:
            continue
        for cand in _variants(float(original)):
            trial = copy.deepcopy(inv)
            _set(trial, cell, cand)
            tids = identities(trial)
            if tids and all(t["ok"] for t in tids):
                corroborated = [t["label"] for t in tids if cell in t["cells"]]
                fixes.append(dict(cell=cell, old=original, new=cand, corroborated_by=corroborated, inv=trial))
    if not fixes:
        return None, "no single OCR-style correction makes the invoice consistent"
    distinct = {(f["cell"], f["new"]) for f in fixes}
    if len(distinct) > 1:
        opts = "; ".join(f"{_cell_name(f['cell'], inv)} → {fmt_inr(f['new'])}" for f in fixes[:4])
        return None, f"ambiguous: {len(distinct)} different corrections would work ({opts})"
    f = fixes[0]
    return f["inv"], dict(cell=_cell_name(f["cell"], inv), path=list(f["cell"]), old=f["old"], new=f["new"],
                          corroborated_by=f["corroborated_by"])


def _cell_name(cell, inv):
    if cell[0] == "lines":
        names = {"qty": "quantity", "unit_price": "rate", "amount": "amount"}
        return f"Line {cell[1] + 1} {names[cell[2]]}"
    if cell[0] == "taxes":
        return inv["taxes"][cell[1]]["type"]
    if cell[0] == "adjustments":
        return inv["adjustments"][cell[1]]["label"] or "Adjustment"
    return {"subtotal": "Subtotal", "total": "Total"}[cell[0]]
