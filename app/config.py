"""Business rules in one place, so an AP lead can read and change them without touching logic."""
import os
from datetime import date

# Price / amount tolerance vs the PO: an invoice may exceed the PO by at most the SMALLER of these.
TOLERANCE_PCT = 1.0          # % of the expected (PO-priced) amount
TOLERANCE_ABS = 5000.00      # rupees

ROUNDING = 1.00              # rupees allowed for rounding when checking the invoice's own arithmetic
GST_RATES = {0, 0.25, 3, 5, 12, 18, 28, 40}
MAX_INVOICE_AGE_DAYS = 180   # older invoices go to review (possible stale / re-used document)

MIN_FIELD_CONFIDENCE = 0.60  # below this, a key field is treated as unreliable -> review
REPAIR_MIN_CORROBORATION = 3 # an OCR repair must be confirmed by this many independent identities to auto-pass

LINE_MATCH_MIN = 0.50        # description similarity needed to pair an invoice line with a PO line
PO_INFER_MIN = 0.55          # score needed to suggest a PO when the invoice has none

APPROVER = "AP manager"      # who gives the final OK when the agent recommends approval


def human_approval() -> bool:
    """A person gives the final OK on every invoice: the agent only recommends, and nothing is posted or paid
    until someone approves. APPROVAL_MODE=auto lets the agent approve clean invoices on its own."""
    return os.getenv("APPROVAL_MODE", "human").strip().lower() != "auto"


STAGE_DELAY = float(os.getenv("STAGE_DELAY", "0.45"))  # seconds; makes the live view watchable in a demo


def today() -> date:
    v = os.getenv("DEMO_TODAY")
    return date.fromisoformat(v) if v else date.today()
