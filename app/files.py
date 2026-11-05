"""What arrives is not always a clean PDF: phone photos, scans saved as images, password-protected or broken
files. Turn what can be read into a PDF, and explain plainly what can't."""
from __future__ import annotations

import io

import pdfplumber
from PIL import Image, ImageOps

IMAGE_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"II*\x00", b"MM\x00*", b"BM", b"GIF8")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp")


class Unreadable(ValueError):
    """The file can't be read. The message is written for the person who has to deal with it."""


def is_image(name: str, data: bytes) -> bool:
    return data.startswith(IMAGE_MAGIC) or (data[:4] == b"RIFF" and data[8:12] == b"WEBP") \
        or (name or "").lower().endswith(IMAGE_EXT) and not data.startswith(b"%PDF")


def looks_like_document_photo(data: bytes) -> bool:
    """Emails carry logos and signature images. Only a big image can be a photo or scan of an invoice."""
    try:
        w, h = Image.open(io.BytesIO(data)).size
    except Exception:
        return False
    return max(w, h) >= 1000 and min(w, h) >= 600


def image_to_pdf(data: bytes) -> bytes:
    """A photo or image scan becomes a one-page (or multi-page TIFF) PDF; the reader then OCRs it."""
    try:
        img = Image.open(io.BytesIO(data))
        frames = []
        for i in range(getattr(img, "n_frames", 1)):
            img.seek(i)
            f = ImageOps.exif_transpose(img.copy()).convert("RGB")  # phone photos carry their rotation in EXIF
            long_side = max(f.size)
            if long_side < 1600:  # small photos: enlarge so OCR has enough pixels per character
                k = 1600 / long_side
                f = f.resize((round(f.width * k), round(f.height * k)), Image.LANCZOS)
            frames.append(f)
    except Exception as e:
        raise Unreadable("This image can't be opened. It may be damaged; ask for the invoice again, ideally as a PDF.") from e
    out = io.BytesIO()
    frames[0].save(out, "PDF", resolution=200.0, save_all=True, append_images=frames[1:])
    return out.getvalue()


def check_pdf(data: bytes) -> int:
    """Pages in the PDF, or Unreadable with a plain reason."""
    if b"/Encrypt" in data[:200000] or b"/Encrypt" in data[-20000:]:
        try:  # many 'protected' PDFs only restrict printing and open without a password
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                n = len(pdf.pages)
                pdf.pages[0].chars
            return n
        except Exception:
            raise Unreadable("This PDF is password-protected, so it can't be read. Ask the vendor for a copy "
                             "without a password.") from None
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            n = len(pdf.pages)
            if n:
                pdf.pages[0].chars
    except Exception:
        raise Unreadable("This file is damaged or isn't a complete PDF. Ask the vendor to send it again.") from None
    if not n:
        raise Unreadable("This PDF has no pages.")
    return n


def normalise(name: str, data: bytes) -> tuple[str, bytes, str | None]:
    """(filename, pdf bytes, note). Raises Unreadable with a reason a person can act on."""
    if not data:
        raise Unreadable("The file is empty.")
    if data.startswith(b"%PDF"):
        check_pdf(data)
        return name, data, None
    if is_image(name, data):
        pdf = image_to_pdf(data)
        base = (name or "invoice").rsplit(".", 1)[0]
        return f"{base}.pdf", pdf, f"Converted from an image ({name}) and read with OCR"
    raise Unreadable("That file isn't a PDF or an image of an invoice. Upload the invoice as a PDF, or a photo "
                     "(JPG/PNG) of it.")


def friendly_error(e: Exception) -> str:
    """For anything that still fails inside the pipeline: say what to do, not what crashed."""
    if isinstance(e, Unreadable):
        return str(e)
    t = f"{type(e).__name__}: {e}"
    if "Encrypt" in t or "password" in t.lower():
        return "This PDF is password-protected, so it can't be read. Ask the vendor for a copy without a password."
    if "PdfminerException" in t or "PDFSyntaxError" in t or "No /Root" in t or "PdfiumError" in t:
        return "This file is damaged or isn't a complete PDF. Ask the vendor to send it again."
    if "TesseractNotFound" in t or "tesseract is not installed" in t.lower():
        return "This is a scanned invoice, and OCR (Tesseract) isn't installed on this computer, so it can't be read."
    return f"Something went wrong reading this invoice ({type(e).__name__}). A person needs to look at it."
