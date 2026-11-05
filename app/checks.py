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
from .config import GST_RATES, ROUNDING
from .utils import fmt_inr

OCR_CONFUSIONS = {
    "0": "86", "1": "47", "2": "7", "3": "85", "4": "19", "5": "63", "6": "850", "7": "12",
    "8": "0365", "9": "40",
}


def _close(a, b, tol=ROUNDING):
    return a is not None and b is not None and abs(a - b) <= tol


def effective_subtotal(inv: dict):
    """Pre-tax amount. For tax-inclusive invoices it is derived: total - tax."""
    tax = sum(t["amount"] or 0 for t in inv["taxes"])
    if inv.get("tax_inclusive"):
        return round(inv["total"] - tax, 2) if inv.get("total") is not None else None
    if inv.get("subtotal") is not None:
        return inv["subtotal"]
    if inv["lines"]:
        return round(sum(l["amount"] or 0 for l in inv["lines"]), 2)
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
        s = round(sum(l["amount"] or 0 for l in lines), 2)
        if inclusive:
            out.append(dict(id="lines_total", label="Lines add up to the (tax-inclusive) total",
                            expected=s, found=total, ok=_close(s, total),
                            cells=[("lines", i, "amount") for i in range(len(lines))] + [("total",)]))
        elif inv.get("subtotal") is not None:
            out.append(dict(id="lines_subtotal", label="Lines add up to the subtotal",
                            expected=s, found=inv["subtotal"], ok=_close(s, inv["subtotal"]),
                            cells=[("lines", i, "amount") for i in range(len(lines))] + [("subtotal",)]))

    tax_sum = round(sum(t["amount"] or 0 for t in taxes), 2)
    if not inclusive and inv.get("subtotal") is not None and total is not None:
        calc = round(inv["subtotal"] + tax_sum, 2)
        out.append(dict(id="total", label="Subtotal + tax = total", expected=calc, found=total,
                        ok=_close(calc, total),
                        cells=[("subtotal",)] + [("taxes", j, "amount") for j in range(len(taxes))] + [("total",)]))

    base = effective_subtotal(inv)
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
    out = []
    for t in inv["taxes"]:
        if t.get("rate") is None:
            continue
        r = t["rate"] * (2 if t["type"] in ("CGST", "SGST") else 1)
        if r not in GST_RATES:
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
    return {"subtotal": "Subtotal", "total": "Total"}[cell[0]]
