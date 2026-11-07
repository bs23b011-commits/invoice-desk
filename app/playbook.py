"""Exception playbook: for every kind of problem, who owns it, the one question they must answer,
how long they have, and who it escalates to. Finance edits this table, not the code.

A handoff = category + owner + evidence + what the agent already tried + ONE specific question + deadline.
"""
from __future__ import annotations

from .utils import fmt_inr

PROC, VMT, AP, CTRL, TAX, CAT = ("Procurement (PO owner)", "Vendor master team", "AP reviewer",
                                 "Finance controller", "Tax team", "Category manager")


def _f(facts, k, default="-"):
    v = (facts or {}).get(k)
    return default if v in (None, "") else v


PLAYBOOK = {
    # ---- purchase order --------------------------------------------------------------------------
    "PO_QTY_OVER": dict(category="Billed more than ordered", owner=PROC, sla_h=48, escalate=CAT,
                        q=lambda f, c: f"Did we order the extra {_f(f, 'extra')} × {_f(f, 'item')} outside "
                                       f"{c.get('po_number')}? Amend the PO, or ask the vendor for a credit note?"),
    "PO_BALANCE": dict(category="Billed more than ordered", owner=PROC, sla_h=48, escalate=CAT,
                       q=lambda f, c: f"{c.get('po_number')} has {_f(f, 'remaining')} left but this invoice is "
                                      f"{_f(f, 'invoice')}. Increase the PO, or ask for a credit note?"),
    "PO_PRICE": dict(category="Price above PO", owner=PROC, sla_h=120, escalate=CAT,
                     q=lambda f, c: "Is the new rate agreed with the vendor? Approve the variance, or request a credit "
                                    f"note for {_f(f, 'variance', 'the difference')}?"),
    "PO_MISSING": dict(category="No PO on invoice", owner=PROC, sla_h=48, escalate=CAT,
                       q=lambda f, c: f"Is this invoice for {_f(f, 'po')}? Confirm and it will be posted against it."),
    "PO_NOT_FOUND": dict(category="No matching PO", owner=PROC, sla_h=48, escalate=CAT,
                         q=lambda f, c: "Who ordered this? Raise a PO, or confirm it is approved non-PO spend."),
    "PO_INVALID": dict(category="PO reference wrong", owner=PROC, sla_h=48, escalate=CAT,
                       q=lambda f, c: "Which PO does this invoice belong to?"),
    "PO_ITEM": dict(category="Item not on PO", owner=PROC, sla_h=48, escalate=CAT,
                    q=lambda f, c: f"Was '{_f(f, 'item')}' ordered? Add it to the PO or reject the line."),
    "PO_CLOSED": dict(category="PO closed", owner=PROC, sla_h=48, escalate=CAT,
                      q=lambda f, c: f"{c.get('po_number')} is closed. Reopen it or reject the invoice?"),
    "GRN_SHORT": dict(category="Goods not received yet", owner="Stores / receiving", sla_h=48, escalate=PROC,
                      q=lambda f, c: f"Have all {_f(f, 'billed')} × {_f(f, 'item')} arrived? The warehouse shows "
                                     f"{_f(f, 'received')}."),
    # ---- the document itself --------------------------------------------------------------------------
    "PAGES_UNREAD": dict(category="Pages not read", owner=AP, sla_h=24, escalate=CTRL,
                         q=lambda f, c: f"Only {_f(f, 'read')} of {_f(f, 'pages')} pages could be read. Do the lines "
                                        "and the total on the invoice match what is shown here?"),
    "MIXED_PDF": dict(category="Several vendors in one PDF", owner=AP, sla_h=24, escalate=CTRL,
                      q=lambda f, c: f"This invoice arrived in one PDF with invoices from other vendors "
                                     f"({', '.join(f.get('vendors') or [])}). Is it genuine and meant for us?"),
    "FOREIGN_CURRENCY": dict(category="Foreign-currency invoice", owner=CTRL, sla_h=48, escalate=CTRL,
                             q=lambda f, c: f"This invoice is in {_f(f, 'currency')}. Pay it as a foreign remittance "
                                            "outside this desk, or reject it?"),
    # ---- vendor ------------------------------------------------------------------------------------
    "VENDOR_UNKNOWN": dict(category="Vendor not onboarded", owner=VMT, sla_h=72, escalate=CTRL,
                           q=lambda f, c: f"Is '{c.get('vendor_name')}' a vendor we use? Start onboarding (KYC, bank "
                                          "verification) or reject."),
    "VENDOR_BLOCKED": dict(category="Vendor blocked", owner=VMT, sla_h=72, escalate=CTRL,
                           q=lambda f, c: f"Has {c.get('vendor_name')} completed re-verification? Until then the invoice "
                                          "stays rejected."),
    "GSTIN_INVALID": dict(category="GSTIN invalid", owner=TAX, sla_h=72, escalate=CTRL,
                          q=lambda f, c: "Ask the vendor for a corrected invoice with a valid GSTIN?"),
    "GSTIN_MISSING": dict(category="GSTIN missing", owner=TAX, sla_h=72, escalate=CTRL,
                          q=lambda f, c: "Ask the vendor to reissue the invoice with their GSTIN?"),
    "NAME_MISMATCH": dict(category="Vendor name mismatch", owner=VMT, sla_h=48, escalate=CTRL,
                          q=lambda f, c: "Has the vendor changed its legal name? Update the master or reject."),
    "BUYER_GSTIN": dict(category="Our GSTIN missing", owner=TAX, sla_h=72, escalate=CTRL,
                        q=lambda f, c: "Ask the vendor to reissue with our GSTIN so input tax credit can be claimed?"),
    "TAX_TYPE": dict(category="Wrong tax type", owner=TAX, sla_h=72, escalate=CTRL,
                     q=lambda f, c: "Ask the vendor to reissue with the correct GST type (IGST vs CGST+SGST)?"),
    "IRN_MISSING": dict(category="No e-invoice (IRN)", owner=TAX, sla_h=72, escalate=CTRL,
                        q=lambda f, c: "Has the vendor sent the e-invoice? Without an IRN we can't claim the input tax "
                                       "credit, so payment waits for it."),
    "IRN_MISMATCH": dict(category="IRN doesn't match invoice", owner=TAX, sla_h=48, escalate=CTRL,
                         q=lambda f, c: "The IRN belongs to another document. Ask the vendor for the correct e-invoice?"),
    "GST_RATE": dict(category="Invalid GST rate", owner=TAX, sla_h=72, escalate=CTRL,
                     q=lambda f, c: "Which GST rate applies to these items?"),
    # ---- fraud -------------------------------------------------------------------------------------
    "BANK_CHANGE": dict(category="Bank details changed", owner=VMT, sla_h=24, escalate=CTRL,
                        q=lambda f, c: f"Call {_f(f, 'vendor')} on the number already on file "
                                       f"({_f(f, 'phone', 'in the vendor master')}), not one on this invoice: did they "
                                       f"really move from {_f(f, 'on_file')} to {_f(f, 'bank')}?"),
    "IMPERSONATION": dict(category="Possible impersonation", owner=VMT, sla_h=24, escalate=CTRL,
                          q=lambda f, c: f"Is this really {_f(f, 'vendor')}? Verify the GSTIN with them by phone "
                                         "before anything is paid."),
    "AMOUNT_ANOMALY": dict(category="Unusual amount", owner=AP, sla_h=48, escalate=CTRL,
                           q=lambda f, c: "Is an invoice this large expected from this vendor? Who approved the spend?"),
    "FRAUD_HIGH": dict(category="High fraud risk", owner=CTRL, sla_h=4, escalate="CFO",
                       q=lambda f, c: "Freeze payments to this bank account and confirm the vendor's identity "
                                      "through the contact on file. Report if fraudulent."),
    # ---- duplicates --------------------------------------------------------------------------------
    "DUPLICATE": dict(category="Duplicate invoice", owner=AP, sla_h=48, escalate=CTRL,
                      q=lambda f, c: f"Confirm this repeats {_f(f, 'original')} and send the drafted note to the "
                                     "vendor?"),
    "POSSIBLE_DUPLICATE": dict(category="Possible duplicate", owner=AP, sla_h=48, escalate=CTRL,
                               q=lambda f, c: f"Is this a separate bill, or the same one as {_f(f, 'original')} "
                                              "with a new number?"),
    # ---- reading -----------------------------------------------------------------------------------
    "OCR_UNREPAIRABLE": dict(category="Scan unreadable", owner=AP, sla_h=24, escalate=CTRL,
                             q=lambda f, c: "Please read the flagged amounts from the scan and correct them."),
    "REPAIR_WEAK": dict(category="OCR fix needs a check", owner=AP, sla_h=24, escalate=CTRL,
                        q=lambda f, c: f"Is {_f(f, 'cell')} really {_f(f, 'new')} on the paper?"),
    "OCR_QUALITY": dict(category="Poor scan", owner=AP, sla_h=24, escalate=CTRL,
                        q=lambda f, c: "Request a clearer copy from the vendor?"),
    "FIELD_MISSING": dict(category="Missing information", owner=AP, sla_h=24, escalate=CTRL,
                          q=lambda f, c: f"What is the {_f(f, 'field')} on this invoice?"),
    "VENDOR_ARITH": dict(category="Vendor's maths is wrong", owner=AP, sla_h=48, escalate=CTRL,
                         q=lambda f, c: "Send the drafted note asking the vendor for a corrected invoice?"),
    "NO_ARITH": dict(category="Numbers can't be cross-checked", owner=AP, sla_h=24, escalate=CTRL,
                     q=lambda f, c: "Please confirm the line items and total against the paper."),
    # ---- dates -------------------------------------------------------------------------------------
    "DATE_ISSUE": dict(category="Date problem", owner=AP, sla_h=48, escalate=CTRL,
                       q=lambda f, c: "Is the invoice date correct? Ask the vendor to reissue if not."),
}
DEFAULT = dict(category="Needs a check", owner=AP, sla_h=48, escalate=CTRL,
               q=lambda f, c: "Please review the flagged point and approve or reject.")


def entry(code: str | None) -> dict:
    return PLAYBOOK.get(code or "", DEFAULT)


def fmt_money_facts(facts: dict) -> dict:
    out = dict(facts or {})
    for k in ("remaining", "invoice", "variance"):
        if isinstance(out.get(k), (int, float)):
            out[k] = fmt_inr(out[k])
    return out
