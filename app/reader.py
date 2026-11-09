"""Stage 1-2: turn a PDF into positioned words and text lines.

Digital PDFs: read the text layer (exact).
Scanned PDFs (no usable text layer): render the page and OCR it with Tesseract.
Both paths output the same structure (words with boxes in PDF points), so everything
downstream is identical - the extractor never needs to know where the words came from.
"""
from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, field

import logging

import pdfplumber
import pypdfium2 as pdfium

logging.getLogger("pdfminer").setLevel(logging.ERROR)  # font-descriptor warnings on some PDFs are harmless

OCR_SCALE = 300 / 72  # render at 300 dpi for OCR


@dataclass
class Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    conf: float = 100.0
    page_box: tuple | None = None  # (x0, top, x1, bottom) on the page as shown, if different from layout coords
    page: int = 0  # 0 = first page (the one shown); later pages are stacked below it in layout coordinates


@dataclass
class Line:
    words: list[Word]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def top(self) -> float:
        return min(w.top for w in self.words)

    @property
    def x0(self) -> float:
        return self.words[0].x0


@dataclass
class Document:
    source: str  # "text-layer" | "ocr"
    pages: int
    words: list[Word]
    lines: list[Line] = field(default_factory=list)
    sha256: str = ""
    ocr_confidence: float | None = None
    preview_png: bytes = b""
    char_count: int = 0
    skew_deg: float = 0.0
    page_w: float = 595.0
    page_h: float = 842.0
    has_hindi: bool = False
    ocr_lang: str = ""
    pages_read: int = 1
    hidden_text: str = ""        # text in the PDF a person can't see (white or microscopic): never read as data
    ocr_reason: str = ""         # why the page was OCR'd although it had a text layer
    rotated: int = 0             # degrees the scan was turned to read it (90/180/270), 0 if upright

    def box(self, words) -> list | None:
        """Bounding box of some words as fractions of the page, for highlighting on the preview."""
        words = [w for w in words if w is not None and w.page == 0]  # only page 1 is shown for highlighting
        if not words or self.rotated:  # a turned scan: OCR boxes don't sit on the preview, so don't draw wrong ones
            return None
        bs = [w.page_box or (w.x0, w.top, w.x1, w.bottom) for w in words]
        x0, y0 = min(b[0] for b in bs), min(b[1] for b in bs)
        x1, y1 = max(b[2] for b in bs), max(b[3] for b in bs)
        pad = 1.5
        return [round(max(0, (x0 - pad) / self.page_w), 4), round(max(0, (y0 - pad) / self.page_h), 4),
                round(min(1, (x1 + pad) / self.page_w), 4), round(min(1, (y1 + pad) / self.page_h), 4)]

    @property
    def text(self) -> str:
        return "\n".join(l.text for l in self.lines)


def group_lines(words: list[Word], tol: float = 3.5) -> list[Line]:
    words = sorted(words, key=lambda w: (round(w.top), w.x0))
    lines: list[list[Word]] = []
    for w in words:
        mid = (w.top + w.bottom) / 2
        for ln in lines:
            lmid = sum((x.top + x.bottom) / 2 for x in ln) / len(ln)
            if abs(mid - lmid) <= tol:
                ln.append(w)
                break
        else:
            lines.append([w])
    out = [Line(sorted(l, key=lambda w: w.x0)) for l in lines]
    return sorted(out, key=lambda l: l.top)


MAX_PAGES = 10


def _render(pdf_bytes: bytes, scale: float):
    doc = pdfium.PdfDocument(pdf_bytes)
    return doc[0].render(scale=scale).to_pil()


def inspect(pdf_bytes: bytes) -> dict:
    """Cheap first look: fingerprint, page count, size of the text layer, and a preview image."""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        n_pages = len(pdf.pages)
        chars = len(pdf.pages[0].chars)
    buf = io.BytesIO()
    _render(pdf_bytes, 1.6).convert("RGB").save(buf, "PNG", optimize=True)
    return dict(sha256=hashlib.sha256(pdf_bytes).hexdigest(), pages=n_pages, chars=chars,
                has_text=chars >= 40, preview_png=buf.getvalue())


def _is_white(c) -> bool:
    col = c.get("non_stroking_color")
    if col is None:
        return False
    if not isinstance(col, (tuple, list)):
        col = (col,)
    try:
        v = [float(x) for x in col]
    except (TypeError, ValueError):
        return False
    if len(v) == 1:
        return v[0] >= 0.97
    if len(v) == 3:
        return min(v) >= 0.97
    if len(v) == 4:  # CMYK
        return max(v) <= 0.03
    return False


def _visible(page, pdf_bytes: bytes, index: int):
    """Drop characters a person reading the paper can't see: microscopic (< 2 pt), or white ink on white paper.
    White text on a dark letterhead band is visible, so the rendered page behind each white character is checked."""
    tiny = lambda c: float(c.get("size") or 0) < 2.0
    whites = [c for c in page.chars if _is_white(c) and not tiny(c)]
    hidden_ids = {id(c) for c in page.chars if tiny(c)}
    if whites:
        try:
            import numpy as np
            img = np.asarray(pdfium.PdfDocument(pdf_bytes)[index].render(scale=1).to_pil().convert("L"))
            h, w = img.shape
            for c in whites:
                x0, x1 = max(0, int(c["x0"]) - 1), min(w, int(c["x1"]) + 1)
                y0, y1 = max(0, int(c["top"]) - 1), min(h, int(c["bottom"]) + 1)
                patch = img[y0:y1, x0:x1]
                if patch.size == 0 or float(patch.mean()) > 235:  # nothing dark behind it: invisible on paper
                    hidden_ids.add(id(c))
        except Exception:
            pass
    if not hidden_ids:
        return page, ""
    hidden = [c for c in page.chars if id(c) in hidden_ids]
    keep = lambda o: not (o.get("object_type") == "char" and any(
        o["x0"] == c["x0"] and o["top"] == c["top"] and o["text"] == c["text"] for c in hidden))
    return page.filter(keep), "".join(c["text"] for c in hidden)


def text_quality(words: list[str]) -> float:
    """Share of words that look like real text (3+ letters, or a number). A scanner's broken OCR layer or a PDF
    with a broken font gives strings like '¤;¬1al!¬~' - those score near 0."""
    if not words:
        return 0.0
    real = sum(1 for w in words if re.fullmatch(r"[A-Za-z\u0900-\u097F][A-Za-z\u0900-\u097F.,:'&()/-]{2,}|"
                                                 r"[(₹Rs.]*\d[\d,./:\-]*\)?%?|[A-Z]{2,}[\w/\-]*\d[\w/\-]*", w))
    return real / len(words)


def read_pdf(pdf_bytes: bytes, force_ocr: bool = False, preview: bool = True) -> Document:
    sha = hashlib.sha256(pdf_bytes).hexdigest()
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        n_pages = len(pdf.pages)
        page, hidden = _visible(pdf.pages[0], pdf_bytes, 0)
        raw = page.extract_words(keep_blank_chars=False, use_text_flow=False, x_tolerance=1.5)
        chars = len(page.chars)
        junk = chars >= 40 and text_quality([w["text"] for w in raw]) < 0.45
        pw, ph = float(page.width), float(page.height)
        more = []  # digital multi-page invoices: line items and totals often continue on page 2+
        for i, pg in enumerate(pdf.pages[1:MAX_PAGES], start=1):
            if len(pg.chars) < 40:
                break  # a scanned later page: not read (flagged by the pipeline)
            off = sum(float(x.height) for x in pdf.pages[:i])
            pg, h2 = _visible(pg, pdf_bytes, i)
            hidden += h2
            more += [Word(w["text"], w["x0"], w["x1"], w["top"] + off, w["bottom"] + off, page=i)
                     for w in pg.extract_words(keep_blank_chars=False, use_text_flow=False, x_tolerance=1.5)]

    buf = io.BytesIO()
    if preview:
        _render(pdf_bytes, 1.6).convert("RGB").save(buf, "PNG", optimize=True)

    if chars >= 40 and not force_ocr and not junk:
        words = [Word(w["text"], w["x0"], w["x1"], w["top"], w["bottom"]) for w in raw] + more
        doc = Document("text-layer", n_pages, words, sha256=sha, preview_png=buf.getvalue(), char_count=chars)
        doc.pages_read = 1 + len({w.page for w in more})
    else:
        doc = _ocr(pdf_bytes, n_pages, sha)
        doc.preview_png = buf.getvalue()
        doc.char_count = chars
        if junk:
            doc.ocr_reason = "its text layer is gibberish (a scanner's failed OCR or a broken font), so the page image was read instead"
    doc.hidden_text = re.sub(r"\s+", " ", hidden).strip()
    doc.page_w, doc.page_h = pw, ph
    doc.lines = group_lines(doc.words)
    return doc


def estimate_skew(img) -> float:
    """Angle (degrees) that makes text rows horizontal: maximise variance of row ink sums."""
    import numpy as np
    from PIL import Image

    small = img.resize((max(1, img.width // 4), max(1, img.height // 4)))
    ink = Image.fromarray(((np.asarray(small) < 140) * 255).astype("uint8"))
    best_ang, best_score = 0.0, -1.0
    for ang in np.arange(-3.0, 3.01, 0.1):
        rows = np.asarray(ink.rotate(float(ang))).sum(axis=1)
        score = float(rows.var())
        if score > best_score:
            best_ang, best_score = float(ang), score
    return round(best_ang, 1)


OCR_NUM_FIXES = [
    (re.compile(r"(?<=\d)\.(?=\d{3}[.,]\d)"), ","),   # 96.400.00 -> 96,400.00 (dot read for comma)
    (re.compile(r"(?<=\d),(?=\d{2}$)"), "."),          # 8,676,00 -> 8,676.00 at end of token
    (re.compile(r"(?<=\d)[oO](?=\d)|(?<=\d)[oO]$"), "0"),
]


def _clean_ocr_token(t: str) -> str:
    if re.search(r"\d", t) and re.fullmatch(r"[\d.,oO]+", t):
        for pat, rep in OCR_NUM_FIXES:
            t = pat.sub(rep, t)
    return t


def hindi_available() -> bool:
    import pytesseract
    try:
        return "hin" in pytesseract.get_languages(config="")
    except Exception:
        return False


def _rotation_inverse(w: int, h: int, angle: float):
    """Map a pixel in PIL's rotate(angle, expand=True) output back to the input image (same maths as PIL)."""
    import math
    a = -math.radians(angle)
    m = [round(math.cos(a), 15), round(math.sin(a), 15), 0.0, round(-math.sin(a), 15), round(math.cos(a), 15), 0.0]
    t = lambda x, y: (m[0] * x + m[1] * y + m[2], m[3] * x + m[4] * y + m[5])
    cx, cy = w / 2.0, h / 2.0
    m[2], m[5] = t(-cx, -cy)
    m[2] += cx
    m[5] += cy
    xs, ys = zip(*(t(x, y) for x, y in ((0, 0), (w, 0), (w, h), (0, h))))
    nw = math.ceil(max(xs)) - math.floor(min(xs))
    nh = math.ceil(max(ys)) - math.floor(min(ys))
    m[2], m[5] = t(-(nw - w) / 2.0, -(nh - h) / 2.0)
    return t


def _ocr(pdf_bytes: bytes, n_pages: int, sha: str) -> Document:
    import pytesseract
    from PIL import Image, ImageOps

    img = _render(pdf_bytes, OCR_SCALE).convert("L")
    turned = orientation(img)
    if turned:
        img = img.rotate(-turned, expand=True, fillcolor=255)  # Tesseract says how far to turn it clockwise
    skew = estimate_skew(img)
    to_page = None
    if abs(skew) >= 0.2:
        to_page = _rotation_inverse(img.width, img.height, skew)
        img = img.rotate(skew, resample=Image.BICUBIC, expand=True, fillcolor=255)
    img = ImageOps.autocontrast(img, cutoff=1)
    def run(lang):
        return pytesseract.image_to_data(img, lang=lang, config="--psm 6", output_type=pytesseract.Output.DICT)

    # English first. Only if English OCR is unsure, check whether the page is Hindi: a Hindi pass that finds a
    # real amount of confident Devanagari text wins; otherwise English stays (Hindi models hurt English pages).
    data, lang = run("eng"), "eng"
    eng_conf = [float(c) for t, c in zip(data["text"], data["conf"]) if (t or "").strip() and float(c) >= 0]
    if eng_conf and sum(eng_conf) / len(eng_conf) < 70 and hindi_available():
        hin = run("eng+hin")
        deva = sum(1 for t, c in zip(hin["text"], hin["conf"])
                   if re.search(r"[\u0900-\u097F]", t or "") and float(c) > 60)
        if deva >= 10:
            data, lang = hin, "eng+hin"
    words, confs = [], []
    for i, txt in enumerate(data["text"]):
        txt = _clean_ocr_token((txt or "").strip())
        conf = float(data["conf"][i])
        if not txt or conf < 0:
            continue
        x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        s = OCR_SCALE  # pixels -> PDF points so layout thresholds are the same as the text-layer path
        pb = None
        if to_page:  # where this word sits on the original (skewed) page, for highlighting
            pts = [to_page(cx, cy) for cx, cy in ((x, y), (x + w, y), (x, y + h), (x + w, y + h))]
            pb = (min(p[0] for p in pts) / s, min(p[1] for p in pts) / s,
                  max(p[0] for p in pts) / s, max(p[1] for p in pts) / s)
        words.append(Word(txt, x / s, (x + w) / s, y / s, (y + h) / s, conf, pb))
        confs.append(conf)
    mean = round(sum(confs) / len(confs), 1) if confs else 0.0
    doc = Document("ocr", n_pages, words, sha256=sha, ocr_confidence=mean)
    doc.skew_deg = skew
    doc.ocr_lang = lang
    doc.rotated = turned
    return doc


def orientation(img) -> int:
    """0/90/180/270: how far a scan must be turned clockwise to be upright. Scanners are fed sideways and
    upside down all the time. Uses Tesseract's orientation detector; only trusted when it is confident."""
    import pytesseract
    try:
        osd = pytesseract.image_to_osd(img, config="--psm 0")
    except Exception:
        return 0
    m = re.search(r"Rotate:\s*(\d+)", osd)
    c = re.search(r"Orientation confidence:\s*([\d.]+)", osd)
    if not m or not c:
        return 0
    rot, conf = int(m.group(1)) % 360, float(c.group(1))
    return rot if rot and conf >= 2.0 else 0
