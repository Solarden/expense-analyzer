"""Loan attachments (Phase 21): query layer + the upload/download/delete routes.

Query-layer tests run on ``db_session``; HTTP tests use ``auth_client`` (both share
the temp engine, and the attachments dir is redirected to a temp path in conftest).
The behaviours that matter: type is decided by the bytes not the browser, the size
limit holds, a download is forced as an attachment and can't be reached via the
wrong loan id, and deleting a loan (or a single doc) takes its files with it.
"""

from collections.abc import Callable

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlmodel import Session

from expense_analyzer import attachments
from expense_analyzer.config import get_settings
from expense_analyzer.models import Account, AccountType, Loan
from expense_analyzer.queries.planning import loan_documents as dq
from expense_analyzer.queries.planning import loans as lq

PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + b"x" * 64
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"y" * 64


def _store_path(loan_id: int, stored_name: str):
    return attachments.document_path(get_settings().attachments_path, loan_id, stored_name)


# --- query layer -----------------------------------------------------------


def test_create_list_get_document(
    db_session: Session,
    make_account: Callable[..., Account],
    make_loan: Callable[..., Loan],
):
    acc = make_account(name="Mortgage", type=AccountType.loan)
    loan = make_loan(account_id=acc.id)

    doc = dq.create_document(
        db_session,
        loan_id=loan.id,
        filename="contract.pdf",
        stored_name="abc.pdf",
        content_type="application/pdf",
        size_bytes=123,
    )

    assert dq.get_document(db_session, doc.id).filename == "contract.pdf"
    assert [d.id for d in dq.list_documents(db_session, loan.id)] == [doc.id]
    # Another loan's list doesn't see it.
    assert dq.list_documents(db_session, loan.id + 999) == []


def test_delete_loan_removes_its_document_rows(
    db_session: Session,
    make_account: Callable[..., Account],
    make_loan: Callable[..., Loan],
):
    acc = make_account(name="Mortgage", type=AccountType.loan)
    loan = make_loan(account_id=acc.id)
    dq.create_document(
        db_session,
        loan_id=loan.id,
        filename="c.pdf",
        stored_name="s.pdf",
        content_type="application/pdf",
        size_bytes=1,
    )

    assert lq.delete_loan(db_session, loan.id) is True
    assert dq.list_documents(db_session, loan.id) == []


# --- HTTP: upload ----------------------------------------------------------


def test_upload_stores_file_and_lists_it(
    auth_client: TestClient,
    db_session: Session,
    make_account: Callable[..., Account],
    make_loan: Callable[..., Loan],
):
    acc = make_account(name="Mortgage", type=AccountType.loan)
    loan = make_loan(account_id=acc.id)

    resp = auth_client.post(
        f"/dashboard/loans/{loan.id}/documents",
        files={"file": ("contract.pdf", PDF, "application/pdf")},
        follow_redirects=False,
    )
    assert resp.status_code == status.HTTP_303_SEE_OTHER

    docs = dq.list_documents(db_session, loan.id)
    assert len(docs) == 1
    assert docs[0].filename == "contract.pdf"
    assert docs[0].content_type == "application/pdf"
    assert docs[0].size_bytes == len(PDF)
    # The bytes really landed on disk under the generated name.
    assert _store_path(loan.id, docs[0].stored_name).read_bytes() == PDF
    # The document shows on the detail page.
    assert "contract.pdf" in auth_client.get(f"/dashboard/loans/{loan.id}").text


def test_upload_trusts_bytes_not_declared_type(
    auth_client: TestClient,
    db_session: Session,
    make_account: Callable[..., Account],
    make_loan: Callable[..., Loan],
):
    """PDF bytes sent with a lying content-type / name are accepted as a PDF; the
    on-disk name is generated, so the ``.exe`` name can't reach the filesystem."""
    acc = make_account(name="Mortgage", type=AccountType.loan)
    loan = make_loan(account_id=acc.id)

    resp = auth_client.post(
        f"/dashboard/loans/{loan.id}/documents",
        files={"file": ("evil.exe", PDF, "application/octet-stream")},
        follow_redirects=False,
    )
    assert resp.status_code == status.HTTP_303_SEE_OTHER
    doc = dq.list_documents(db_session, loan.id)[0]
    assert doc.content_type == "application/pdf"
    assert doc.stored_name.endswith(".pdf")
    assert ".exe" not in doc.stored_name


def test_upload_rejects_unsupported_type(
    auth_client: TestClient,
    db_session: Session,
    make_account: Callable[..., Account],
    make_loan: Callable[..., Loan],
):
    acc = make_account(name="Mortgage", type=AccountType.loan)
    loan = make_loan(account_id=acc.id)

    resp = auth_client.post(
        f"/dashboard/loans/{loan.id}/documents",
        files={"file": ("notes.txt", b"just some text", "text/plain")},
        follow_redirects=False,
    )
    assert resp.status_code == status.HTTP_303_SEE_OTHER
    assert "error=" in resp.headers["location"]
    assert dq.list_documents(db_session, loan.id) == []


def test_upload_rejects_empty_file(
    auth_client: TestClient,
    db_session: Session,
    make_account: Callable[..., Account],
    make_loan: Callable[..., Loan],
):
    acc = make_account(name="Mortgage", type=AccountType.loan)
    loan = make_loan(account_id=acc.id)

    resp = auth_client.post(
        f"/dashboard/loans/{loan.id}/documents",
        files={"file": ("empty.pdf", b"", "application/pdf")},
        follow_redirects=False,
    )
    assert resp.status_code == status.HTTP_303_SEE_OTHER
    assert dq.list_documents(db_session, loan.id) == []


def test_upload_rejects_oversize_file(
    auth_client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    make_account: Callable[..., Account],
    make_loan: Callable[..., Loan],
):
    # Shrink the limit instead of generating a 10 MB payload.
    monkeypatch.setattr(get_settings(), "attachment_max_bytes", 16)
    acc = make_account(name="Mortgage", type=AccountType.loan)
    loan = make_loan(account_id=acc.id)

    resp = auth_client.post(
        f"/dashboard/loans/{loan.id}/documents",
        files={"file": ("big.pdf", PDF, "application/pdf")},  # > 16 bytes
        follow_redirects=False,
    )
    assert resp.status_code == status.HTTP_303_SEE_OTHER
    assert "error=" in resp.headers["location"]
    assert dq.list_documents(db_session, loan.id) == []


def test_upload_sanitizes_stored_filename(
    auth_client: TestClient,
    db_session: Session,
    make_account: Callable[..., Account],
    make_loan: Callable[..., Loan],
):
    acc = make_account(name="Mortgage", type=AccountType.loan)
    loan = make_loan(account_id=acc.id)

    # A filename carrying path components is reduced to a clean basename (control
    # chars are stripped too — covered directly in tests/unit/test_attachments.py,
    # since an HTTP client percent-encodes them in the multipart header in transit).
    auth_client.post(
        f"/dashboard/loans/{loan.id}/documents",
        files={"file": ("../../etc/contract.pdf", PDF, "application/pdf")},
    )
    doc = dq.list_documents(db_session, loan.id)[0]
    assert doc.filename == "contract.pdf"


def test_upload_rejects_when_loan_at_document_cap(
    auth_client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    make_account: Callable[..., Account],
    make_loan: Callable[..., Loan],
):
    monkeypatch.setattr(get_settings(), "attachment_max_per_loan", 1)
    acc = make_account(name="Mortgage", type=AccountType.loan)
    loan = make_loan(account_id=acc.id)

    first = auth_client.post(
        f"/dashboard/loans/{loan.id}/documents",
        files={"file": ("a.pdf", PDF, "application/pdf")},
        follow_redirects=False,
    )
    assert first.status_code == status.HTTP_303_SEE_OTHER

    second = auth_client.post(
        f"/dashboard/loans/{loan.id}/documents",
        files={"file": ("b.pdf", PDF, "application/pdf")},
        follow_redirects=False,
    )
    assert second.status_code == status.HTTP_303_SEE_OTHER
    assert "error=" in second.headers["location"]
    # The second upload was rejected — still exactly one document.
    assert len(dq.list_documents(db_session, loan.id)) == 1


def test_upload_404_for_missing_loan(auth_client: TestClient):
    resp = auth_client.post(
        "/dashboard/loans/9999/documents",
        files={"file": ("c.pdf", PDF, "application/pdf")},
        follow_redirects=False,
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND


# --- HTTP: download --------------------------------------------------------


def test_download_returns_bytes_as_attachment(
    auth_client: TestClient,
    db_session: Session,
    make_account: Callable[..., Account],
    make_loan: Callable[..., Loan],
):
    acc = make_account(name="Mortgage", type=AccountType.loan)
    loan = make_loan(account_id=acc.id)
    auth_client.post(
        f"/dashboard/loans/{loan.id}/documents",
        files={"file": ("schedule.png", PNG, "image/png")},
    )
    doc = dq.list_documents(db_session, loan.id)[0]

    resp = auth_client.get(f"/dashboard/loans/{loan.id}/documents/{doc.id}")
    assert resp.status_code == status.HTTP_200_OK
    assert resp.content == PNG
    assert resp.headers["content-type"] == "image/png"
    assert "attachment" in resp.headers["content-disposition"]
    assert "schedule.png" in resp.headers["content-disposition"]


def test_download_404_for_wrong_loan(
    auth_client: TestClient,
    db_session: Session,
    make_account: Callable[..., Account],
    make_loan: Callable[..., Loan],
):
    acc = make_account(name="Mortgage", type=AccountType.loan)
    loan = make_loan(account_id=acc.id)
    other = make_loan(account_id=acc.id)
    auth_client.post(
        f"/dashboard/loans/{loan.id}/documents",
        files={"file": ("c.pdf", PDF, "application/pdf")},
    )
    doc = dq.list_documents(db_session, loan.id)[0]

    # The doc exists, but under `loan`, not `other` — the mismatched path 404s.
    resp = auth_client.get(f"/dashboard/loans/{other.id}/documents/{doc.id}")
    assert resp.status_code == status.HTTP_404_NOT_FOUND


def test_download_404_for_missing_doc(
    auth_client: TestClient,
    make_account: Callable[..., Account],
    make_loan: Callable[..., Loan],
):
    acc = make_account(name="Mortgage", type=AccountType.loan)
    loan = make_loan(account_id=acc.id)
    assert (
        auth_client.get(f"/dashboard/loans/{loan.id}/documents/9999").status_code
        == status.HTTP_404_NOT_FOUND
    )


# --- HTTP: delete ----------------------------------------------------------


def test_delete_document_removes_row_and_file(
    auth_client: TestClient,
    db_session: Session,
    make_account: Callable[..., Account],
    make_loan: Callable[..., Loan],
):
    acc = make_account(name="Mortgage", type=AccountType.loan)
    loan = make_loan(account_id=acc.id)
    auth_client.post(
        f"/dashboard/loans/{loan.id}/documents",
        files={"file": ("c.pdf", PDF, "application/pdf")},
    )
    doc = dq.list_documents(db_session, loan.id)[0]
    path = _store_path(loan.id, doc.stored_name)
    assert path.exists()

    resp = auth_client.post(
        f"/dashboard/loans/{loan.id}/documents/{doc.id}/delete", follow_redirects=False
    )
    assert resp.status_code == status.HTTP_303_SEE_OTHER
    assert dq.list_documents(db_session, loan.id) == []
    assert not path.exists()


def test_delete_loan_over_http_removes_files(
    auth_client: TestClient,
    db_session: Session,
    make_account: Callable[..., Account],
    make_loan: Callable[..., Loan],
):
    acc = make_account(name="Mortgage", type=AccountType.loan)
    loan = make_loan(account_id=acc.id)
    auth_client.post(
        f"/dashboard/loans/{loan.id}/documents",
        files={"file": ("c.pdf", PDF, "application/pdf")},
    )
    doc = dq.list_documents(db_session, loan.id)[0]
    path = _store_path(loan.id, doc.stored_name)
    assert path.exists()

    auth_client.post(f"/dashboard/loans/{loan.id}/delete", follow_redirects=False)
    assert not path.exists()


def test_document_routes_require_login(client: TestClient):
    # Unauthenticated access redirects to the login page (303), not a 200/404.
    resp = client.get("/dashboard/loans/1/documents/1", follow_redirects=False)
    assert resp.status_code == status.HTTP_303_SEE_OTHER
    assert resp.headers["location"] == "/login"
