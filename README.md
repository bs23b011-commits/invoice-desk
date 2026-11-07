# Invoice Desk: an accounts-payable digital employee

Zamp AI Solutions Analyst case study, **PS-1 (Finance / AP)**.

An invoice arrives (email, vendor form or upload). Invoice Desk reads it, proves the numbers, checks it against the vendor master, earlier invoices, the purchase order, goods receipts, fraud signals and Indian tax rules, and **recommends** a decision with its reasons. When something is wrong, it asks the right person **one specific question**, reads their reply and re-checks. **A person gives the final OK on every invoice** in the *Waiting for you* screen (or from the Approve/Reject buttons in Slack and email). Only then is it posted to the ERP, read back to confirm, and paid in a batch.

| Agent recommends | Meaning | What the person does |
|---|---|---|
| **APPROVE** | Every check passed. Shows what will happen: ERP posting, amount net of TDS, payment date (within the MSME limit), bank account on file. | Approve → posted and scheduled. Or reject. |
| **HOLD** | Each problem becomes a handoff with an owner, evidence, what the agent already did, one question, and a deadline that escalates. The agent writes to whoever can answer and re-checks on the reply. | Wait for the answer, or decide now. A bank-detail change can only be approved after ticking "I called the vendor on the number on file". |
| **REJECT** | Must not be paid: duplicate, blocked vendor, high fraud risk, or the vendor's own maths is wrong. Suspected fraud is never answered. | Confirm reject. Approving anyway needs a written reason. |

`APPROVAL_MODE=auto` switches back to the agent approving clean invoices on its own.

**The idea: AI reads, rules decide, the invoice must agree with itself, and every number can be traced to the paper.**

## Proof that it works

`python scripts/evaluate.py` generates 50 fresh invoices with known answers, in 10 situations, 3 layouts, digital and fax-quality scans. Result (full report in `docs/EVALUATION.md`):

| | |
|---|---|
| Decision accuracy | **100%** |
| False approvals (bad invoice paid) | **0** |
| Silent errors (approved with a misread field) | **0** |
| Single-digit errors above ₹1 caught by the arithmetic check | **100%** of 125 injected |
| Good invoices still sent to a person | 5 of 10, all fax-quality scans where OCR was unsure of the invoice number. Conservative by design. |

**222 automated tests** (`STAGE_DELAY=0 python -m pytest -q`): 68 for Jeen, 7 for the invoice discussion, 9 for several invoices in one PDF, 15 for strange files and your own data (`tests/test_judge_kit.py`). All five n8n workflows tested live in n8n 1.123.

## The 11 stages (live on screen for every invoice)

1. **Receive.** Fingerprint, page count, text layer or not.
2. **Read.** Text layer, or deskew + OCR. English first, Hindi OCR switched on only when the page is in Hindi.
3. **Extract.** Fields by label (English and Hindi), labels learned from reviewers, and an optional LLM text reader. On unclear scans, an optional **vision reader** gives a second opinion. Every field records how it was found, a confidence, and **where it is on the page** (click it to highlight).
4. **Self-check.** qty × rate = amount, lines = subtotal, subtotal + tax = total, tax = rate × base, CGST = SGST. On scans, a misread digit is repaired only if it's the single correction that makes every identity hold.
5. **Vendor.** GSTIN (checksum) and name against the master, blocked status, billed to us, correct GST type.
6. **Duplicate.** Normalised invoice number (INV-0042 = INV42), plus same vendor/amount/date.
7. **PO and goods receipt (3-way match).** Price within tolerance, quantity left on the PO, **goods actually received** (services exempt), PO balance, and a suggested PO when none is quoted (never auto-approved).
8. **Fraud & risk.** Bank account vs master, "new bank details" wording, look-alike name with another GSTIN, amount far above history, urgency, round amounts. These combine into a 0–100 score: 70+ blocks payment, and one heavy signal holds it for a phone check on the number already on file.
9. **Tax & compliance (India).**
   - **E-invoice IRN** must be present for vendors above ₹5 crore. It is recomputed from GSTIN, financial year and invoice number, which catches a copied IRN and also a reviewer's typo.
   - **TDS** under s.393(1) of the Income-tax Act 2025: contractors 1%/2% by PAN type above ₹30k / ₹1 lakh, technical services 2% above ₹50k, on the pre-GST value.
   - **MSME 45-day** payment cap.
   - **GSTR-2B** input-tax-credit status.
10. **Recommendation.** Any fail → reject, any warn → hold with handoffs, otherwise approve. A person makes the final call.
11. **Post to ERP** (after the person approves). GL coding (rules + learned), a balanced journal (expense, input GST, payable net of TDS, TDS payable), posted and **read back field by field**, never twice.

## Waiting for you: the approval screen

The home screen lists every invoice that needs a person (fraud first, then holds, then approvals; MSME at the top). For the selected invoice it shows:

- **The agent's recommendation** in one plain sentence, coloured green / amber / red.
- **3-way match as three boxes:** ordered, received, billed. The one that doesn't agree turns red.
- **A fraud-risk gauge** (0–100) with the signals behind it.
- **"Why":** one card per check (maths, vendor, duplicate, 3-way, fraud, tax) with its result.
- **Who can resolve it**, their question and deadline, and the **conversation** as chat bubbles. A demo button simulates their reply.
- **Approve / Reject**, a note for the audit trail, and who is deciding. After approval it shows the posting and the payment being scheduled, step by step.
- Keys: `J`/`K` next/previous, `A` approve, `R` reject. Light/dark switch in the top bar.

## Jeen, the AP assistant

A chat panel on every screen (**Ask Jeen** bottom-right, or `Ctrl`+`K`). The agent does the work; Jeen
explains it and prepares actions. **Jeen never approves, rejects, pays or edits anything** — there is no
tool in `app/jeen.py` that can, and a test asserts it stays that way.

Ask it things an AP clerk actually asks:

| | |
|---|---|
| Waiting / status | What's waiting for me? · Why is Kaveri on hold? · Who are we waiting on, and since when? · What happens if I approve this? |
| Vendors / money | What do we owe Brightline? · Is Rapid Print an MSME? Are they blocked? · Did we already receive invoice SPS/26-27/0418? |
| Purchase orders | What's left on PO-2026-0104? · Which invoices were billed against it? · Have the goods arrived? |
| Payments / tax | What's due this week? · How much TDS do we deposit, and by when? |
| Rules | What's the price tolerance? · What is a 3-way match? · How does the fraud score work? |
| Prepare (never do) | Draft a reminder to Stores · Prepare approval with note "…" · Open payments |

How it is built:

- **13 tools over the existing records** — ten read-only (`search_invoices`, `get_invoice`,
  `vendor_summary`, `po_balance`, `payments_due`, `waiting_list`, `duplicate_check`, `explain_rule`,
  `dashboard_stats`, `activity`) and three prepare-only (`draft_email`, `open_screen`,
  `prepare_decision`). The prepare tools return a button; nothing happens until you click it.
- **Every fact comes from a tool result**, with a source chip under the answer that opens that invoice,
  PO or vendor. `explain_rule` reads the live numbers out of `config.py` / `compliance.py` / `risk.py`
  rather than repeating them, so the answer cannot drift from the rule the pipeline applies.
- **Two modes.** With `GEMINI_API_KEY` or `ANTHROPIC_API_KEY` it uses tool-calling (up to 5 chained calls,
  15 s budget, last 10 messages as memory) and the badge reads **AI**. With no key, or if the call fails
  or times out, it falls back to a keyword/regex intent router over the *same* tools and the badge reads
  **Offline mode**. The demo works with no key at all.
- **Prompt injection.** Invoice text, email replies and vendor names are data: they are wrapped in
  `<untrusted>` delimiters, the system prompt forbids following instructions inside them, and anything
  that reads like an instruction to an assistant is reported to you as suspicious instead of obeyed.
- **Refuses to decide.** "Just approve it" gets a polite no and the offer to fill in the form for you.
- Every question and the tools it used are written to the Activity log (`kind="jeen"`).
- Answers in Hindi if you write in Hindi.

**Measured, not asserted:** `python scripts/eval_jeen.py` asks all 31 questions against a fixed desk and
writes `docs/JEEN_EVAL.md`. Latest run, rules mode (no key):

| | |
|---|---|
| Correct answers | **31/31** |
| Wrong numbers (a rupee figure not in any tool result) | **0** |
| Actions taken without a human | **0** |

The LLM path has only been exercised against a fake provider in tests, like the rest of the LLM features
here — **not yet run against a live key**.

## After the recommendation: the agent acts

- **Writes to people.** Vendor-fixable problems (missing PO, missing IRN, wrong GSTIN, duplicate) go to the vendor's address **on file**. Internal facts go to the team that knows ("Stores: did the 300 helmets arrive?"). Suspected fraud never contacts the vendor.
- **Reads replies.** These arrive through n8n by their `[Ref …]` tag, or in the UI's reply box for demos. Replies are understood by rules (optional LLM for unclear ones). The agent supplies the new fact (PO number, goods received, IRN) and **re-runs every check**. Procurement can approve or reject by reply. Strangers replying to a thread are ignored.
- **Escalates.** Unanswered questions go to the next owner after their deadline (24 h for bank changes, 48 h for receipts, and so on).
- **Learns.**
  - A reviewer types a field the reader missed. The system finds it on the page and remembers that vendor's label, so the next invoice from them is read automatically.
  - Reviewers can also change a GL code, and that coding is remembered.
- **Pays.** The Payments tab batches approved invoices (MSME first, overdue highlighted), produces the bank upload file, marks them paid, and shows TDS to deposit by the 7th.

## Test invoices (`samples/`, generated by `scripts/make_data.py`)

| # | Situation | Outcome |
|---|---|---|
| 01 | Clean invoice | Approved, posted, paid on terms |
| 02 | Freight, rate +0.5% (within tolerance) | Approved, 2% TDS deducted |
| 03 | GST-inclusive prices, MSME vendor | Approved, tax backed out, due date capped at 45 days, pH meter capitalised |
| 04 | Annual PO billed quarterly | Approved, 2% TDS on technical services |
| 05 | **Fax scan, OCR misreads CGST 8,676 as 6,676** | Approved after a provable repair (3 identities agree) |
| 06 | **Split PO over-billed** | On hold. Procurement is asked and can approve by reply. |
| 07 | **Duplicate re-sent as "INV42"** | Rejected. Vendor told it was already processed. |
| 08 | **No PO number** | On hold. The vendor is asked; when they reply with the PO, it's re-checked and approved. |
| 09 | Vendor blocked (KYC expired) | Rejected |
| 10 | **Real vendor, new bank account** | On hold, risk 60. Call the number on file within 24 h. |
| 11 | **Look-alike vendor** | Rejected, risk 100. Payment frozen, impostor not contacted. |
| 12 | **Billed before goods arrived** | On hold. Stores is asked; "the balance arrived" books the receipt and approves. |
| 13 | **Large vendor, no e-invoice IRN** | On hold. The vendor sends the IRN, which is verified and approved. |
| 14 | **New layout the reader doesn't know** | On hold. The reviewer types 2 fields once and the layout is learned. |
| 15 | Same new layout again | Approved automatically after #14 |
| 16 | **Invoice in Hindi** | Approved. Same checks, no special path. |

All of this is fictional data: 8 vendors, 13 POs, goods receipts, invoice history and GSTR-2B. **Reset demo data** restores the starting state.

## Several invoices in one PDF

The first thing the agent does with a file is look at **every page on its own** (its text layer, or OCR if the page is a scan) and work out where each invoice starts: a new invoice number, or another vendor's GSTIN under a new heading. Then:

| The PDF holds | What happens |
|---|---|
| One invoice (one or more pages) | Checked as usual |
| Several invoices from **one vendor** | Split; each checked fully against **its own PO** and gets its own recommendation. Invoices on the same PO use up its balance in order |
| Invoices from **different vendors** | Split and checked the same way, and each is **held for a person** even if every check passes: one vendor doesn't normally send another's invoice |
| Scanned and digital pages mixed | Each page read its own way, then split as above |
| The same invoice printed again ("duplicate for transporter") | Counted once; the copies are noted |
| More than 10 pages | Held for a person, never half-read |

The agent says what it found the moment the file arrives ("This PDF contains 3 invoices from Kaveri…"), each card in *Waiting for you* shows "2 of 3 in bills.pdf", and the note starts each invoice's discussion.

## The invoice discussion (@mentions and @Jeen)

Every invoice has one discussion, like comments on a shared document, in *Waiting for you*. It shows the whole story in order: when it arrived, the agent's emails and the replies, each re-check, the decision, and the team's comments, with Jeen's two-line summary pinned at the top.

- **@Jeen** answers inside the thread, with source links ("@Jeen why is this on hold?", "@Jeen what do we owe Kaveri?"). It can explain and draft, never approve or pay.
- **@Stores, @Procurement, @AP, @Controller, @Manager** send the comment to that team by email with the invoice's `[Ref …]` tag. Their reply comes back into the thread through n8n, the agent understands it ("received 100 more") and re-checks the invoice.
- **@Vendor** writes to the vendor's address **on file**, except on a suspicious invoice (bank change, look-alike vendor), where it refuses and says to call the number in the vendor master.
- Typing `@` suggests who can be mentioned; Ctrl+Enter posts.

## Testing it with your own invoices

The demo's vendors and POs are fictional, so an invoice from your own supplier stops at "vendor not onboarded" (correctly: nobody unknown gets paid). Two ways to check it fully:

1. **Records → Use your own vendors, POs and goods receipts.** Download the templates, fill them (or export from your ERP), upload vendors, then POs, then receipts. CSV or Excel; column names like "GST No", "Qty", "Rate" are understood. Bad rows are listed and skipped. Your data survives "Reset demo data"; "Remove my imported data" takes it out.
2. **Onboard from the hold.** On a "vendor not onboarded" invoice in *Waiting for you*, fill in the vendor form (bank account confirmed with the vendor, not copied from the invoice) and the agent re-checks.

**Strange files are handled, not crashed on:**

| File | What happens |
|---|---|
| Phone photo or image scan (JPG, PNG, TIFF, WEBP) | Turned into a page and read with OCR |
| Password-protected or damaged PDF | Plain reason ("ask the vendor for a copy without a password"). From email it still appears in *Waiting for you*, so it can't get lost |
| Logo or signature image in an email | Ignored |
| Foreign-currency invoice | Held for the finance controller |
| Totals on page 2 | Read (digital PDFs, up to 5 pages) |

## Run it

```bash
# Python 3.11+, Tesseract (Windows: winget install UB-Mannheim.TesseractOCR | macOS: brew install tesseract tesseract-lang)
pip install -r requirements.txt
uvicorn app.main:app --reload             # http://localhost:8000
STAGE_DELAY=0 python -m pytest -q         # 222 tests
python scripts/evaluate.py                # accuracy report -> docs/EVALUATION.md + dashboard
python scripts/eval_jeen.py               # Jeen's 31 questions -> docs/JEEN_EVAL.md
```

**Optional settings** (environment variables):

| Variable | Purpose |
|---|---|
| `GEMINI_API_KEY` or `ANTHROPIC_API_KEY` | LLM text reader, vision reader for scans, reply understanding, and Jeen's tool-calling mode. Built and tested with stand-ins, **not yet run against a live key**. Without one, Jeen answers from its rules router and the readers fall back to rules. |
| `GEMINI_MODEL`, `ANTHROPIC_MODEL` | override the model (defaults `gemini-2.5-flash`, `claude-sonnet-5-5`) |
| `INTAKE_TOKEN`, `PUBLIC_URL`, `N8N_WEBHOOK_URL`, `APP_SECRET` | n8n connection (see `integrations/n8n/README.md`) |
| `APPROVAL_MODE` | `human` (default): a person gives the final OK on every invoice. `auto`: the agent approves clean invoices on its own |
| `INTERNAL_EMAIL_DOMAINS` | whose email replies may approve or confirm (default `yourcompany.com`) |
| `STAGE_DELAY` | pause between stages for the live view (default 0.45 s; not counted in processing time) |

**Deploy:** the `Dockerfile` installs Tesseract with Hindi. For Hugging Face Spaces, use the Docker SDK and push the repo. For Render, `render.yaml` is included.

## n8n (`integrations/n8n/`, five importable workflows)

**Why n8n here:** connectors are commodity plumbing that a finance team can re-point without a developer. The decision rules need tests, an audit trail and version control, so they stay in code. If n8n is down, nothing is lost: every message the app would have sent is in the **Activity** log.

1. **Email intake:** Gmail → each PDF → `/api/intake`
2. **Vendor upload form:** no credentials needed, the easiest to demo
3. **Notify decisions and send agent messages:** Slack + email, with one-click **signed** Approve/Reject links (they expire, work once, and a bank change needs a confirmed phone call)
4. **Escalation timer:** hourly
5. **Read replies:** `[Ref …]` replies go back to the agent

## A 5-minute demo order

1. **01 clean:** the 11 stages run live. Click a field to see it on the paper. Then in **Waiting for you**, read the agent's reasons and approve: watch it post and schedule payment.
2. **05 fax:** the OCR misread is repaired, and the proof is shown.
3. **10 bank change:** risk score, plus the phone-check question.
4. **12 goods not received:** in Waiting for you, the 3-way boxes show 200 received in red. Click "Stores: the rest arrived": it books the receipt, re-checks, and turns green. You approve.
5. **14 → 15:** type two fields once, then the next invoice is read automatically.
6. **Dashboard:** straight-through rate, exceptions by owner, and the accuracy panel (0 false approvals).
7. **Payments:** batch, bank file, TDS to deposit.
8. **Ask Jeen** (bottom-right, or `Ctrl`+`K`): "What's waiting for me?", "Why is Kaveri on hold?",
   "What's left on PO-2026-0104?". Then try "just approve it" and watch it refuse and offer the form
   instead.

## Assumptions (change in `app/config.py`, `app/compliance.py`, `app/playbook.py`)

- **The buyer:** Meridian Foods, Chennai, GSTIN state 33. Amounts in INR. POs are pre-tax.
- **Price tolerance:** the smaller of 1% or ₹5,000 above PO prices.
- **OCR repairs:** an OCR repair needs ≥3 independent identities to pass without review. Arithmetic tolerance is ₹1 (round-off).
- **Guesses go to a person:** a guessed PO, or a field read only by the vision model, always goes to review.
- **Tax rates:** TDS rates and thresholds follow the Income-tax Act 2025 (s.393) as published for tax year 2026-27. **Verify against current law before relying on them.**
- **IRN check is offline:** the IRN is recomputed offline. Live verification on the IRP needs a GSP connection.
- **Simulated pieces:** GSTR-2B, the ERP ("Mock ERP") and the bank are local stand-ins with the same interfaces a real Zoho Books / Tally / ERPNext adapter or bank API would use.
- **Pages:** an invoice is read up to 10 pages (digital). On a scanned invoice only its first page is OCR'd, so a multi-page scan goes to a person; a PDF over 10 pages also goes to a person, never half-read. Fax-quality Hindi scans usually go to a person; digital Hindi invoices are automatic.
- **Currency:** INR only. An invoice in USD, EUR, GBP and so on is held for the finance controller.

## What I'd build next

- Live GSP connection for IRN and GSTR-2B
- A real ERP adapter (Zoho Books / ERPNext) behind the same find / post / read interface
- OCR of every page of a multi-page scan
- Vendor credit-note matching
- Running the vision reader with a key on the evaluation set, to measure how many of the held scans it would release

## Code map

```
app/reader.py         text layer or deskew + OCR (adaptive English/Hindi) -> positioned words
app/extract_rules.py  label-based extraction (English + Hindi skeleton matching), GSTIN OCR fixes, field locations
app/extract_llm.py    optional LLM text reader, vision reader, reply classifier
app/learning.py       learn labels from reviewer corrections, apply them per vendor
app/checks.py         arithmetic identities + provable OCR repair
app/matching.py       vendor, duplicates, PO + goods receipt (3-way), dates
app/risk.py           fraud signals and risk score
app/compliance.py     IRN, TDS, MSME, GSTR-2B
app/playbook.py       exception type -> owner, question, SLA, escalation
app/decision.py       approve / hold / reject + grouped handoffs
app/agent.py          messages to vendors and teams, reply understanding, re-checks
app/cockpit.py        "Waiting for you": recommendation, reasons, 3-way match, owner, conversation per invoice
app/erp.py            GL coding, journal, post + read-back, payment batches, bank file
app/integrations.py   intake, webhook, signed one-click links, escalations
app/pipeline.py       runs the 11 stages, records everything
app/jeen.py           Jeen: 13 read/prepare tools, LLM tool-calling + offline rules router, injection defence
app/main.py           API; static/ is the UI (waiting.js = the approval screen, jeen.js = the assistant)
scripts/              sample data, n8n workflow builder, evaluation harness
integrations/n8n/     five workflows + setup guide
docs/EVALUATION.md    latest accuracy report
docs/JEEN_EVAL.md     Jeen on its 31 questions
```
