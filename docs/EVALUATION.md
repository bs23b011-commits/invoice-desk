# Evaluation report

50 generated invoices (5 per situation, seed 7), 3 layouts, digital and fax-quality scans, each with known correct fields and decision. Run with `python scripts/evaluate.py`.

| Measure | Result | What it means |
|---|---|---|
| Decision accuracy | **100.0%** | approve / hold / reject matched the right answer |
| False approvals | **0** | invoices that should not be paid but were approved |
| Silent errors | **0** | approved with a wrongly read field |
| Missed approvals | 5 of 10 | good invoices sent to a person anyway |
| Arithmetic check | catches **100.0%** of single-digit errors above ₹1 (125 injected) | errors of ₹1 or less sit inside the round-off tolerance by design |
| Scans | 0 of 5 approved, 1 after an OCR repair | |
| Speed | 0.47 s per invoice | |

## By situation

| Situation | Expected | Correct | Outcomes |
|---|---|---|---|
| Correct invoice, explicit PO | APPROVE | 5/5 | APPROVE 5 |
| Correct invoice, fax-quality scan (REVIEW acceptable if unreadable) | APPROVE | 5/5 | REVIEW 5 |
| One line priced 3% above the PO | REVIEW | 5/5 | REVIEW 5 |
| Bills more units than the PO allows | REVIEW | 5/5 | REVIEW 5 |
| PO number missing | REVIEW | 5/5 | REVIEW 5 |
| Earlier invoice re-sent with the number re-formatted | REJECT | 5/5 | REJECT 5 |
| Digital invoice whose total doesn't add up | REJECT | 5/5 | REJECT 5 |
| Asks for payment to a new bank account | REVIEW | 5/5 | REVIEW 5 |
| Vendor blocked in the master | REJECT | 5/5 | REJECT 5 |
| Same-state vendor charges IGST | REVIEW | 5/5 | REVIEW 5 |

## Field accuracy

| Field | Accuracy |
|---|---|
| invoice number | 90.0% |
| invoice date | 100.0% |
| po number | 100.0% |
| vendor | 100.0% |
| subtotal | 100.0% |
| total | 100.0% |
| lines | 100.0% |

| Layout | Field accuracy |
|---|---|
| classic | 100.0% |
| classic (scan) | 85.7% |
| letter | 100.0% |
| modern | 100.0% |
