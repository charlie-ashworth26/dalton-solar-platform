"""
Document extraction — provider boundary.

WHY THIS EXISTS
---------------
services/extraction.get_text() called pytesseract with no guard, and
routes/document_routes.py called that with no guard either. On a machine where
the native tesseract executable is absent (installing the pytesseract PYTHON
package does not install the binary), TesseractNotFoundError propagated to Flask
as a 500 — and because the documents row was INSERTed *after* extraction, the
uploaded file was written to disk but never recorded. The rep lost the upload.

This module makes extraction a provider with an explicit contract:

    DocumentExtractor            abstract boundary
      └── LocalExtractor         pdfplumber + tesseract (today)
      └── AIVisionExtractor      later, no UI or storage change required

Nothing here knows about enrollments, and the route never calls tesseract
directly, so swapping providers touches this file only.

HONEST RESULTS
--------------
ExtractionStatus distinguishes success / partial / unreadable / ocr_unavailable
/ unsupported, so "we could not read it" is never confused with "it is empty".
Confidence is reported ONLY where the underlying tool supplies a defensible
number: tesseract's real per-word confidence from image_to_data. pdfplumber has
no confidence concept, so text-PDF extraction reports None rather than a
fabricated score.
"""
import os
import shutil
import subprocess
import tempfile

# ─────────────── Result model ───────────────

class ExtractionStatus:
    SUCCESS = "success"                    # read it, got the fields we needed
    PARTIAL = "partial"                    # read it, some fields missing/uncertain
    UNREADABLE = "unreadable"              # OCR ran, produced nothing usable
    OCR_UNAVAILABLE = "ocr_unavailable"    # no OCR engine on this machine
    UNSUPPORTED = "unsupported"            # wrong/corrupt file type
    ERROR = "error"                        # anything else, captured not raised

    # Statuses where the rep must supply data themselves for a BILL.
    NEEDS_MANUAL = (UNREADABLE, OCR_UNAVAILABLE, UNSUPPORTED, ERROR)


class ExtractionResult:
    """What an extractor returns. Never raises out of extract()."""

    def __init__(self, status, text="", fields=None, confidence=None,
                 issues=None, provider="local", page_count=0):
        self.status = status
        self.text = text or ""
        self.fields = fields or {}
        # None means "this provider cannot express confidence" - never a guess.
        self.confidence = confidence
        self.issues = issues or []
        self.provider = provider
        self.page_count = page_count

    @property
    def usable(self):
        return self.status in (ExtractionStatus.SUCCESS, ExtractionStatus.PARTIAL)

    @property
    def needs_manual_entry(self):
        return self.status in ExtractionStatus.NEEDS_MANUAL

    def to_dict(self):
        return {
            "status": self.status,
            "fields": self.fields,
            "confidence": self.confidence,
            "issues": self.issues,
            "provider": self.provider,
            "page_count": self.page_count,
            "usable": self.usable,
            "needs_manual_entry": self.needs_manual_entry,
        }


# ─────────────── Capability detection ───────────────

_ocr_probe_cache = None


def ocr_available(refresh=False):
    """Is a usable native tesseract executable actually present?

    The pytesseract PYTHON package installing successfully says nothing about
    the binary. Probing once and caching keeps this off the upload hot path.
    """
    global _ocr_probe_cache
    if _ocr_probe_cache is not None and not refresh:
        return _ocr_probe_cache
    _ocr_probe_cache = _probe_ocr()
    return _ocr_probe_cache


def _probe_ocr():
    override = os.environ.get("DALTON_FORCE_OCR_UNAVAILABLE")
    if override and override.strip().lower() in ("1", "true", "yes"):
        return False   # test hook: simulate a machine with no tesseract
    try:
        import pytesseract
    except Exception:
        return False
    # An explicit path wins, otherwise look on PATH.
    configured = getattr(getattr(pytesseract, "pytesseract", None), "tesseract_cmd", None)
    if configured and configured != "tesseract" and not shutil.which(configured):
        if not os.path.exists(configured):
            return False
    elif not shutil.which(configured or "tesseract"):
        return False
    try:
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def ocr_status():
    """Non-sensitive description for diagnostics."""
    available = ocr_available()
    version = None
    if available:
        try:
            import pytesseract
            version = str(pytesseract.get_tesseract_version())
        except Exception:
            version = None
    return {
        "ocr_available": available,
        "engine": "tesseract" if available else None,
        "version": version,
        "note": None if available else
                "Native tesseract executable not found. Uploads are still stored "
                "and the flow continues; bill fields must be entered manually.",
    }


# ─────────────── Image preprocessing (on a COPY, never the original) ───────────────

MAX_OCR_DIMENSION = 2600     # huge phone photos slow OCR without helping accuracy


def preprocess_for_ocr(src_path, workdir):
    """Return a path to a DERIVATIVE prepared for OCR.

    The stored original is never modified or replaced. If anything here fails we
    fall back to the original rather than risk corrupting the input.

    Applies only corrections that are safe and deterministic:
      * EXIF orientation (phones record rotation rather than rotating pixels)
      * downscale very large images
      * greyscale
    Deliberately NOT applied: deskew and perspective correction. Those need
    OpenCV and, applied to an already-straight page, can make OCR worse. Better
    to fail gracefully than to silently mangle a readable document.
    """
    try:
        from PIL import Image, ImageOps
    except Exception:
        return src_path
    try:
        with Image.open(src_path) as img:
            # exif_transpose honours the orientation tag: sideways and
            # upside-down phone photos become upright.
            out = ImageOps.exif_transpose(img)
            out = out.convert("L")
            w, h = out.size
            if max(w, h) > MAX_OCR_DIMENSION:
                scale = MAX_OCR_DIMENSION / float(max(w, h))
                out = out.resize((max(1, int(w * scale)), max(1, int(h * scale))))
            derived = os.path.join(workdir, "ocr_input.png")
            out.save(derived, format="PNG")
            return derived
    except Exception:
        return src_path


# ─────────────── Provider boundary ───────────────

class DocumentExtractor:
    """Abstract provider. A future AIVisionExtractor subclasses this and the
    enrollment UI, routes and storage model stay unchanged."""

    name = "abstract"

    def extract(self, file_paths, category="utility_bill"):
        """file_paths: ordered list forming ONE logical document set.
        Must return an ExtractionResult and must never raise."""
        raise NotImplementedError


class LocalExtractor(DocumentExtractor):
    """pdfplumber for text PDFs, tesseract for images and scanned PDFs."""

    name = "local"

    def extract(self, file_paths, category="utility_bill"):
        paths = [p for p in (file_paths or []) if p]
        if not paths:
            return ExtractionResult(ExtractionStatus.UNSUPPORTED, provider=self.name,
                                    issues=["No files were provided."])

        texts, issues, confidences, pages = [], [], [], 0
        saw_ocr_unavailable = False

        for path in paths:
            part = self._extract_one(path)
            pages += part.page_count
            issues.extend(part.issues)
            if part.status == ExtractionStatus.OCR_UNAVAILABLE:
                saw_ocr_unavailable = True
            if part.text.strip():
                texts.append(part.text)
            if part.confidence is not None:
                confidences.append(part.confidence)

        combined = "\n".join(texts).strip()
        # Real mean word confidence from tesseract, or None. Never invented.
        confidence = (round(sum(confidences) / len(confidences), 3)
                      if confidences else None)

        if not combined:
            status = (ExtractionStatus.OCR_UNAVAILABLE if saw_ocr_unavailable
                      else ExtractionStatus.UNREADABLE)
            return ExtractionResult(status, provider=self.name, issues=issues,
                                    confidence=confidence, page_count=pages)

        fields = {}
        if category == "utility_bill":
            try:
                from services import extraction as legacy
                parsed = legacy.parse_utility_bill(combined)
                # Keep only values; the legacy per-field "confidence" numbers are
                # hand-tuned constants, not measurements, so they are not surfaced.
                fields = {k: v.get("value") for k, v in parsed.items()
                          if isinstance(v, dict) and v.get("value") not in (None, "")}
            except Exception as e:
                issues.append(f"Field parsing failed: {e}")

        if category == "utility_bill":
            required = ("account_number", "account_holder", "service_street")
            missing = [f for f in required if not fields.get(f)]
            if missing:
                issues.append("Could not read: " + ", ".join(missing))
                status = ExtractionStatus.PARTIAL if fields else ExtractionStatus.UNREADABLE
            else:
                status = ExtractionStatus.SUCCESS
        else:
            status = ExtractionStatus.SUCCESS

        return ExtractionResult(status, text=combined, fields=fields,
                                confidence=confidence, issues=issues,
                                provider=self.name, page_count=pages)

    # ── per-file ──

    def _extract_one(self, path):
        lower = (path or "").lower()
        if lower.endswith(".pdf"):
            return self._extract_pdf(path)
        return self._extract_image(path)

    def _extract_pdf(self, path):
        issues = []
        try:
            import pdfplumber
        except Exception:
            return ExtractionResult(ExtractionStatus.ERROR, provider=self.name,
                                    issues=["pdfplumber is not installed."])
        try:
            parts, page_count = [], 0
            with pdfplumber.open(path) as pdf:
                for page in pdf.pages[:12]:
                    page_count += 1
                    parts.append(page.extract_text() or "")
            text = "\n".join(parts).strip()
        except Exception as e:
            return ExtractionResult(ExtractionStatus.UNSUPPORTED, provider=self.name,
                                    issues=[f"Could not open the PDF: {e}"])

        if text:
            # A text PDF. pdfplumber has no confidence concept - report None.
            return ExtractionResult(ExtractionStatus.SUCCESS, text=text,
                                    provider=self.name, page_count=page_count)

        # Image-only/scanned PDF: render pages and OCR them.
        issues.append("PDF contained no text layer; attempted OCR.")
        return self._ocr_scanned_pdf(path, page_count, issues)

    def _ocr_scanned_pdf(self, path, page_count, issues):
        if not ocr_available():
            return ExtractionResult(ExtractionStatus.OCR_UNAVAILABLE, provider=self.name,
                                    page_count=page_count,
                                    issues=issues + ["OCR engine unavailable for a scanned PDF."])
        try:
            import pdfplumber
            import pytesseract
        except Exception as e:
            return ExtractionResult(ExtractionStatus.OCR_UNAVAILABLE, provider=self.name,
                                    page_count=page_count, issues=issues + [str(e)])
        texts, confs = [], []
        try:
            with tempfile.TemporaryDirectory(prefix="dalton_ocr_") as tmp:
                with pdfplumber.open(path) as pdf:
                    for i, page in enumerate(pdf.pages[:6]):
                        img_path = os.path.join(tmp, f"page_{i}.png")
                        page.to_image(resolution=200).save(img_path)
                        t, c = self._ocr_image_file(img_path, tmp)
                        if t.strip():
                            texts.append(t)
                        if c is not None:
                            confs.append(c)
        except Exception as e:
            return ExtractionResult(ExtractionStatus.UNREADABLE, provider=self.name,
                                    page_count=page_count,
                                    issues=issues + [f"Could not render PDF pages for OCR: {e}"])
        text = "\n".join(texts).strip()
        conf = round(sum(confs) / len(confs), 3) if confs else None
        status = ExtractionStatus.SUCCESS if text else ExtractionStatus.UNREADABLE
        if not text:
            issues.append("OCR of the scanned PDF produced no readable text.")
        return ExtractionResult(status, text=text, confidence=conf,
                                provider=self.name, page_count=page_count, issues=issues)

    def _extract_image(self, path):
        if not ocr_available():
            # THE ORIGINAL 500. Now a controlled status; the caller keeps the file.
            return ExtractionResult(
                ExtractionStatus.OCR_UNAVAILABLE, provider=self.name, page_count=1,
                issues=["No OCR engine is installed on this server, so images "
                        "cannot be read automatically."])
        try:
            with tempfile.TemporaryDirectory(prefix="dalton_ocr_") as tmp:
                text, conf = self._ocr_image_file(path, tmp)
        except Exception as e:
            return ExtractionResult(ExtractionStatus.UNSUPPORTED, provider=self.name,
                                    page_count=1,
                                    issues=[f"Could not read the image: {e}"])
        if not text.strip():
            return ExtractionResult(ExtractionStatus.UNREADABLE, provider=self.name,
                                    page_count=1, confidence=conf,
                                    issues=["No readable text was found in the image."])
        return ExtractionResult(ExtractionStatus.SUCCESS, text=text, confidence=conf,
                                provider=self.name, page_count=1)

    def _ocr_image_file(self, path, workdir):
        """OCR one image. Returns (text, real_mean_word_confidence_or_None).
        Preprocessing operates on a derivative; the original is untouched."""
        import pytesseract
        from PIL import Image
        prepared = preprocess_for_ocr(path, workdir)
        with Image.open(prepared) as img:
            try:
                data = pytesseract.image_to_data(
                    img, output_type=pytesseract.Output.DICT)
                words = [(t, c) for t, c in zip(data.get("text", []), data.get("conf", []))
                         if str(t).strip()]
                text = " ".join(t for t, _ in words)
                # Tesseract's own per-word confidence - a measured value.
                nums = []
                for _, c in words:
                    try:
                        v = float(c)
                    except (TypeError, ValueError):
                        continue
                    if v >= 0:
                        nums.append(v)
                conf = round(sum(nums) / len(nums) / 100.0, 3) if nums else None
                return text, conf
            except Exception:
                return pytesseract.image_to_string(img), None


# ─────────────── Selection ───────────────

_EXTRACTORS = {"local": LocalExtractor}


def get_extractor(name=None):
    """Provider selection. A future AIVisionExtractor registers here and the
    route, UI and storage model are unaffected."""
    key = (name or os.environ.get("DALTON_EXTRACTOR") or "local").strip().lower()
    return _EXTRACTORS.get(key, LocalExtractor)()


def register_extractor(name, cls):
    _EXTRACTORS[name] = cls
