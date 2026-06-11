"""Local file storage for loan attachments (Phase 21).

Loan documents (contracts, repayment schedules, payment proofs) are stored as
plain files in a directory on the app's ``data/`` volume
(see :data:`~expense_analyzer.config.Settings.attachments_path`). Local-only —
nothing leaves the LAN, no OCR (keep-pi-fully-local). This module is the only
place that touches that directory; the DB-side metadata lives in
:class:`~expense_analyzer.models.LoanDocument` (queried via
:mod:`expense_analyzer.queries.planning.loan_documents`).

Two safety rules baked in here:

- **Trust the bytes, not the browser.** A file's type is decided by sniffing its
  magic bytes (:func:`sniff_content_type`), never by the client-supplied
  ``Content-Type`` (which the client controls). An upload whose contents don't
  match an allowed signature is rejected.
- **Generated names, never user input.** The on-disk name is a fresh UUID plus
  the type's canonical extension (:func:`store_loan_document`), so a crafted
  upload filename can't traverse out of the storage directory. The original name
  is kept only as display/download metadata in the DB.
"""

import shutil
import uuid
from pathlib import Path

# Allowed upload types: contracts arrive as PDFs, schedules/proofs as scans or
# phone photos. The map is sniff-signature -> canonical extension and is the
# security allowlist (kept in code, not env): an upload must match one of these
# by content to be accepted. JPEG/PNG/WebP cover camera and screenshot formats.
ALLOWED_ATTACHMENT_TYPES: dict[str, str] = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def allowed_types_label() -> str:
    """Human-readable list of accepted extensions, for the upload hint/error."""
    return ", ".join(sorted(ext.lstrip(".").upper() for ext in ALLOWED_ATTACHMENT_TYPES.values()))


# Cap on the display name kept in the DB — generous for a real filename, but a
# bound so a pathological upload name can't bloat the row or the rendered page.
MAX_DISPLAY_NAME_LEN = 255


def safe_display_name(raw: str, fallback: str) -> str:
    """Clean an upload's own filename into safe display/download metadata.

    Defence-in-depth on top of the generated on-disk name: drop any path
    components (keep the basename only), strip non-printable characters (control
    chars, CR/LF) and surrounding whitespace, and bound the length. Returns
    ``fallback`` (the generated ``stored_name``) when nothing usable is left, so
    the row never carries an empty or hostile name.
    """
    name = Path(raw).name
    name = "".join(ch for ch in name if ch.isprintable()).strip()

    return name[:MAX_DISPLAY_NAME_LEN] or fallback


def sniff_content_type(data: bytes) -> str | None:
    """Return the MIME type of ``data`` by its magic bytes, or None if unsupported.

    Deliberately narrow — only the :data:`ALLOWED_ATTACHMENT_TYPES` formats are
    recognised, so anything else (an executable renamed to ``.pdf``, an HTML page,
    a zip) sniffs to None and is rejected. We never consult the client's declared
    ``Content-Type``.
    """
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    # WebP: "RIFF" <4-byte size> "WEBP".
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"

    return None


def _loan_dir(base: Path, loan_id: int) -> Path:
    """The storage directory for one loan's documents.

    ``loan_id`` is an int from the route path, so it can't inject path segments;
    the per-loan subdirectory just keeps the store tidy and makes deleting a
    loan's files a single ``rmtree``.
    """
    return base / "loan" / str(loan_id)


def store_loan_document(base: Path, loan_id: int, data: bytes, content_type: str) -> str:
    """Write ``data`` under a generated name and return that ``stored_name``.

    The name is a fresh UUID plus the canonical extension for ``content_type``
    (which the caller has already validated via :func:`sniff_content_type`), so it
    is never derived from the upload's filename — no path traversal. The caller
    persists the returned name in :class:`~expense_analyzer.models.LoanDocument`.
    """
    ext = ALLOWED_ATTACHMENT_TYPES[content_type]
    stored_name = f"{uuid.uuid4().hex}{ext}"
    loan_dir = _loan_dir(base, loan_id)
    loan_dir.mkdir(parents=True, exist_ok=True)
    (loan_dir / stored_name).write_bytes(data)

    return stored_name


def document_path(base: Path, loan_id: int, stored_name: str) -> Path:
    """Absolute path to a stored document, for serving a download."""
    return _loan_dir(base, loan_id) / stored_name


def delete_document_file(base: Path, loan_id: int, stored_name: str) -> None:
    """Remove a single stored document file; a missing file is not an error."""
    document_path(base, loan_id, stored_name).unlink(missing_ok=True)


def delete_loan_dir(base: Path, loan_id: int) -> None:
    """Remove a loan's whole document directory (used when deleting the loan)."""
    shutil.rmtree(_loan_dir(base, loan_id), ignore_errors=True)
