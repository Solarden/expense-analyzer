"""Loan-attachment storage helpers (Phase 21): type sniffing and on-disk layout.

Pure filesystem helpers, exercised against a ``tmp_path``. The two things that
matter for safety: the type is decided by the bytes (not a declared name), and the
on-disk name is generated (so a crafted upload name can't traverse out).
"""

from pathlib import Path

from expense_analyzer import attachments

# Minimal valid magic-byte headers for each allowed format.
PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 "


def test_sniff_recognises_each_allowed_type():
    assert attachments.sniff_content_type(PDF) == "application/pdf"
    assert attachments.sniff_content_type(JPEG) == "image/jpeg"
    assert attachments.sniff_content_type(PNG) == "image/png"
    assert attachments.sniff_content_type(WEBP) == "image/webp"


def test_sniff_rejects_unsupported_or_disguised_content():
    # An executable / script / zip renamed to .pdf still sniffs to None.
    assert attachments.sniff_content_type(b"MZ\x90\x00") is None  # PE executable
    assert attachments.sniff_content_type(b"<html><body>hi</body></html>") is None
    assert attachments.sniff_content_type(b"PK\x03\x04") is None  # zip/docx container
    assert attachments.sniff_content_type(b"") is None
    # "RIFF...." that isn't WebP (e.g. a WAV) is not accepted as an image.
    assert attachments.sniff_content_type(b"RIFF\x24\x00\x00\x00WAVEfmt ") is None


def test_store_generates_name_with_canonical_extension(tmp_path: Path):
    stored = attachments.store_loan_document(tmp_path, 7, PDF, "application/pdf")

    assert stored.endswith(".pdf")
    # The name is a generated UUID hex (32 chars) + extension — never user input.
    assert len(stored) == len("0123456789abcdef0123456789abcdef.pdf")
    path = attachments.document_path(tmp_path, 7, stored)
    assert path.read_bytes() == PDF
    # Lands inside the per-loan subdirectory, under the base.
    assert path.parent == tmp_path / "loan" / "7"


def test_two_uploads_get_distinct_names(tmp_path: Path):
    a = attachments.store_loan_document(tmp_path, 1, PNG, "image/png")
    b = attachments.store_loan_document(tmp_path, 1, PNG, "image/png")

    assert a != b


def test_delete_document_file_is_idempotent(tmp_path: Path):
    stored = attachments.store_loan_document(tmp_path, 3, JPEG, "image/jpeg")
    path = attachments.document_path(tmp_path, 3, stored)
    assert path.exists()

    attachments.delete_document_file(tmp_path, 3, stored)
    assert not path.exists()
    # A second delete (missing file) is a no-op, not an error.
    attachments.delete_document_file(tmp_path, 3, stored)


def test_safe_display_name_strips_path_and_control_chars():
    # Path components are dropped (basename only).
    assert attachments.safe_display_name("../../etc/passwd", "fb.pdf") == "passwd"
    assert attachments.safe_display_name("a/b/c/contract.pdf", "fb.pdf") == "contract.pdf"
    # Control characters (incl. CR/LF that could fray a header) are removed.
    assert attachments.safe_display_name("c\r\nontract\t.pdf", "fb.pdf") == "contract.pdf"


def test_safe_display_name_falls_back_when_empty():
    assert attachments.safe_display_name("", "generated.pdf") == "generated.pdf"
    assert attachments.safe_display_name("   ", "generated.pdf") == "generated.pdf"
    # A name that is only path + control chars collapses to the fallback.
    assert attachments.safe_display_name("/\r\n", "generated.pdf") == "generated.pdf"


def test_safe_display_name_bounds_length():
    long = "x" * 1000 + ".pdf"
    out = attachments.safe_display_name(long, "fb.pdf")
    assert len(out) == attachments.MAX_DISPLAY_NAME_LEN


def test_delete_loan_dir_removes_all_files(tmp_path: Path):
    attachments.store_loan_document(tmp_path, 5, PDF, "application/pdf")
    attachments.store_loan_document(tmp_path, 5, PNG, "image/png")
    loan_dir = tmp_path / "loan" / "5"
    assert len(list(loan_dir.iterdir())) == 2

    attachments.delete_loan_dir(tmp_path, 5)
    assert not loan_dir.exists()
    # Removing a loan with no documents is harmless.
    attachments.delete_loan_dir(tmp_path, 999)
