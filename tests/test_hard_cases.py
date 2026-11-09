"""The HARD set: invoices built from real complaints about AP tools (Ramp, Stampli, BILL reviews, Indian GST audits).

Each case in samples_hard/EXPECTED.json says what a careful AP clerk would do. These tests keep the agent there.
Plus unit tests for the 'common sense' module (app/sense.py) and the money-safety rules around it.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["STAGE_DELAY"] = "0"
os.environ.setdefault("RUNTIME_DIR", tempfile.mkdtemp())
os.environ.setdefault("DEMO_TODAY", "2026-11-04")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import pytest  # noqa: E402

from app import checks, pipeline, sense, store  # noqa: E402
from app.reader import read_pdf, text_quality  # noqa: E402

HARD = ROOT / "samples_hard"
CASES = json.loads((HARD / "EXPECTED.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------------------------------- the hard set
def _groups():
    out, cur = [], None
    for c in CASES:
        if c.get("sequence") and cur and cur[0].get("sequence") == c["sequence"]:
            cur.append(c)
        else:
            cur = [c]
            out.append(cur)
    return out


@pytest.mark.parametrize("group", _groups(), ids=lambda g: g[0]["file"].split("_")[0])
def test_hard_case(group):
    import run_hard
    store.init(reset=True)
    for case in group:
        run = run_hard.run_one(case["file"])
        ok, why = run_hard.score(case, run)
        assert ok, f"{case['file']}: {'; '.join(why)} | {(run.get('result') or {}).get('headline')}"


# ------------------------------------------------------------------------------------------- sense.py
def test_amount_in_words_indian_system():
    assert sense.amount_in_words("Amount in words: Rupees One Lakh Thirteen Thousand Seven Hundred Fifty-Two Only")[
        "value"] == 113752
    assert sense.amount_in_words("Rupees Two Crore Five Lakh Only")["value"] == 20500000
    assert sense.amount_in_words("Amount in words: Rupees Twenty and Fifty Paise Only")["value"] == 20.5
    assert sense.amount_in_words("Amount in words: Rupees Gibberish Thousand Only") is None  # never guesses


@pytest.mark.parametrize("head,kind", [
    (["Sundaram Packaging CREDIT NOTE"], "credit_note"),
    (["Apex Lab Instruments PROFORMA INVOICE"], "proforma"),
    (["Kaveri Industrial STATEMENT OF ACCOUNT", "Total outstanding 2,09,450.00"], "statement"),
    (["Quote Master Traders TAX INVOICE", "Grand Total 1,000.00", "Sub Total 900.00"], "invoice"),  # a name, not a title
    (["Murugan Electricals TAX INVOICE", "PURCHASE ORDER NO: PO-2026-0107"], "invoice"),         # a label, not a title
    (["Kaveri TAX INVOICE", "PAYMENT REMINDER - 2nd NOTICE"], "invoice"),   # a re-sent invoice: duplicates catch it
    (["Dear Partner,", "Happy Diwali from all of us"], "not_invoice"),
])
def test_doc_kind(head, kind):
    assert sense.doc_kind(head)["kind"] == kind


def test_doc_kind_negative_total_is_a_credit():
    assert sense.doc_kind(["Vendor TAX INVOICE", "Grand Total (10,030.00)"], "Grand Total (10,030.00)")["kind"] == "credit_note"


def test_adjustments_after_total():
    lines = ["Grand Total (INR) 1,13,752.00", "Less: Advance received (RV-0091) (30,000.00)", "Balance Payable 83,752.00"]
    a = sense.adjustments(lines, lines[0])
    assert a["advance"] == 30000 and a["payable"] == 83752
    lines = ["Grand Total 42,480.00", "Add: Previous balance (INV-0042) 41,300.00", "Total Amount Due 83,780.00"]
    a = sense.adjustments(lines, lines[0])
    assert a["previous"] == 41300 and a["advance"] == 0


def test_pack_size():
    assert sense.pack_size("Corrugated boxes (bundle of 25)") == 25
    assert sense.pack_size("Gloves - 12 pcs per box") == 12
    assert sense.pack_size("Corrugated boxes 5-ply") is None


def test_reverse_charge_detected_only_when_it_applies():
    assert sense.reverse_charge("Tax payable on reverse charge basis: YES (GTA, 5%)")["rate"] == 5
    assert sense.reverse_charge("Whether tax is payable on reverse charge: No") is None


# ------------------------------------------------------------------------------------------- GST 2.0
def _inv(date_iso, rate):
    return dict(invoice_date=date_iso, taxes=[dict(type="CGST", rate=rate / 2, amount=1), dict(type="SGST", rate=rate / 2, amount=1)])


def test_twelve_percent_slab_is_gone_after_gst_2():
    assert checks.rate_findings(_inv("2026-10-29", 12))           # after 22 Sep 2025: held
    assert "GST 2.0" in checks.rate_findings(_inv("2026-10-29", 12))[0]["text"]
    assert not checks.rate_findings(_inv("2025-08-10", 12))       # before: a valid slab
    assert not checks.rate_findings(_inv("2026-10-29", 18))
    assert not checks.rate_findings(_inv("2026-10-29", 40))


def test_mixed_rates_split_over_lines():
    inv = dict(lines=[dict(qty=4, unit_price=23500, amount=94000), dict(qty=10, unit_price=1800, amount=18000)],
               subtotal=112000, total=129820, tax_inclusive=False,
               taxes=[dict(type="IGST", rate=18, amount=16920), dict(type="IGST", rate=5, amount=900)])
    ids = checks.identities(inv)
    assert ids and all(i["ok"] for i in ids), [i for i in ids if not i["ok"]]
    inv["taxes"][1]["amount"] = 2160  # 12% of the reagents, labelled 5%: the split no longer works
    assert not all(i["ok"] for i in checks.identities(inv))


def test_discount_between_lines_and_subtotal():
    inv = dict(lines=[dict(qty=10, unit_price=100, amount=1000)], subtotal=980, total=1156.4, tax_inclusive=False,
               adjustments=[dict(label="Less: discount", amount=-20, after_subtotal=False)],
               taxes=[dict(type="CGST", rate=9, amount=88.2), dict(type="SGST", rate=9, amount=88.2)])
    assert all(i["ok"] for i in checks.identities(inv))


# ------------------------------------------------------------------------------------------- reader
def test_white_text_on_a_dark_band_is_not_hidden():
    d = read_pdf((ROOT / "samples" / "08_edge_kaveri_no_po.pdf").read_bytes(), preview=False)
    assert d.hidden_text == ""


def test_hidden_instructions_are_not_read_as_data():
    d = read_pdf((HARD / "h20_hidden_instructions.pdf").read_bytes(), preview=False)
    assert "AI AGENT" in d.hidden_text
    assert "YES BANK" not in d.text and "pre-approved" not in d.text


def test_junk_text_layer_scores_low():
    assert text_quality(["¤;¬1al!¬~'Ia:a", "§a,!ll1~^°¬¦~", "!1aal;¤¦°"]) < 0.45
    assert text_quality(["Kaveri", "Industrial", "Supplies", "1,59,300.00", "GSTIN", "33AKLPK7712M1ZU"]) > 0.8


# ------------------------------------------------------------------------------------------- money safety
def _process(path: Path) -> dict:
    run = pipeline.new_run(path.name, None)
    pdf = path.read_bytes()
    store.file_path(run["id"], "pdf").write_bytes(pdf)
    pipeline.execute(run, pdf)
    return store.get_run(run["id"])


def test_a_credit_note_cannot_be_approved_for_payment():
    store.init(reset=True)
    run = _process(HARD / "h01_credit_note.pdf")
    with pytest.raises(ValueError, match="isn't a bill"):
        pipeline.resolve(run["id"], "approve", "approving the credit note by mistake")
    assert pipeline.resolve(run["id"], "reject", "credit applied to next payment")["resolution"]["action"] == "REJECT"


def test_approving_an_invoice_with_an_advance_pays_only_the_balance():
    store.init(reset=True)
    run = _process(HARD / "h15_advance_adjusted.pdf")
    assert run["result"]["decision"] == "REVIEW"
    run = pipeline.resolve(run["id"], "approve", "advance RV-0091 confirmed in the books")
    pay = run["result"]["payment"]
    assert pay["advance"] == 30000
    assert abs(pay["amount"] - (113752 - 30000 - pay["tds"])) < 0.01


def test_qr_code_value_must_match_the_page(tmp_path):
    """A genuine e-invoice QR on a page whose total was edited afterwards."""
    import make_data
    from make_hard import draw
    store.init(reset=True)
    inv = make_data.build_invoice("V001", "SPS/26-27/0460", "2026-10-31", "2026-11-30", "PO-2026-0101",
                                  [make_data.ln("Corrugated boxes 5-ply 480x320x300", 1000, 18.50, "4819")])
    real_total = inv["total"]
    inv_shown = dict(inv, lines=[dict(l, qty=1100, amount=20350.0) for l in inv["lines"]], subtotal=20350.0,
                     taxes=[("CGST @ 9%", 1831.5), ("SGST @ 9%", 1831.5)], total=24013.0)

    def drawn(path):  # page with edited figures, QR still carrying the original value
        orig = make_data.draw_irn

        def irn_with_original(c, i, v, *a, **k):
            return orig(c, dict(i, total=real_total), v, *a, **k)
        import make_hard
        make_hard.draw_irn = irn_with_original
        try:
            draw(path, make_data.VBY["V001"], inv_shown)
        finally:
            make_hard.draw_irn = orig
    p = tmp_path / "edited.pdf"
    drawn(p)
    run = _process(p)
    texts = " ".join(f["text"] for s in run["stages"] for f in s["findings"])
    assert "QR code doesn't match the page" in texts
    assert run["result"]["decision"] != "APPROVE"
