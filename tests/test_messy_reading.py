"""Reader gaps found by the messy-invoice stress test (samples_messy, m05-m08)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from app.extract_rules import CURRENCY_RE, ITEM_RE, LABELS, SUBTOTAL_RE, TOTAL_RE  # noqa: E402
from app.utils import parse_date  # noqa: E402
import re  # noqa: E402


@pytest.mark.parametrize("s", ["04-Nov-26", "04 Nov 26", "4/11/26", "November 4th 2026", "4th November 2026",
                               "04-Nov-2026", "04/11/2026", "Nov 4, 2026"])
def test_date_formats(s):  # m07
    assert parse_date(s).isoformat() == "2026-11-04"


@pytest.mark.parametrize("line,is_total", [
    ("Net Payable (INR) 84,960.00", True), ("Total Payable 84,960.00", True), ("Balance Due 84,960.00", True),
    ("Grand Total 84,960.00", True), ("Taxable Amt 72,000.00", False)])
def test_total_labels(line, is_total):  # m06
    assert bool(TOTAL_RE.search(line)) == is_total


@pytest.mark.parametrize("line", ["Taxable Amt 72,000.00", "Taxable Amount 72,000.00", "Sub Total 72,000.00"])
def test_subtotal_labels(line):  # m06
    assert SUBTOTAL_RE.search(line)


@pytest.mark.parametrize("label", ["Bill No.", "Inv #", "Document Number", "Tax Inv. Ref", "Invoice No."])
def test_invoice_number_labels(label):  # m06
    assert any(re.fullmatch(p, label, re.I) for p in LABELS["invoice_number"])


@pytest.mark.parametrize("raw", ["1 Steel helmets 8471 100 Nos Rs. 720.00 Rs. 72,000.00",
                                 "1 Steel helmets 8471 100 Nos ₹720.00 ₹72,000.00",
                                 "1 Steel helmets 8471 100 Nos INR 720.00 INR 72,000.00"])
def test_currency_prefix_in_line_items(raw):  # m08
    m = ITEM_RE.match(re.sub(r"\s+", " ", CURRENCY_RE.sub("", raw)).strip())
    assert m and m["qty"] == "100" and m["rate"] == "720.00" and m["amt"] == "72,000.00"
