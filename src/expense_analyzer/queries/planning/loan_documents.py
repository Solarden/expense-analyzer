"""Loan-document metadata queries — the DB side of loan attachments (Phase 21).

Plain ``Session``-in, model-out functions like the rest of the query layer. The
*files* live on disk and are handled in :mod:`expense_analyzer.attachments`; this
module only stores and reads the :class:`~expense_analyzer.models.LoanDocument`
rows. The route orchestrates the two (write file, then create row; delete row,
then remove file) so each layer stays single-purpose.
"""

from sqlmodel import Session, col, select

from expense_analyzer.models import LoanDocument


def list_documents(session: Session, loan_id: int) -> list[LoanDocument]:
    """All documents for a loan, newest first."""
    return list(
        session.exec(
            select(LoanDocument)
            .where(LoanDocument.loan_id == loan_id)
            .order_by(col(LoanDocument.uploaded_at).desc())
        ).all()
    )


def get_document(session: Session, doc_id: int) -> LoanDocument | None:
    return session.get(LoanDocument, doc_id)


def create_document(
    session: Session,
    *,
    loan_id: int,
    filename: str,
    stored_name: str,
    content_type: str,
    size_bytes: int,
) -> LoanDocument:
    doc = LoanDocument(
        loan_id=loan_id,
        filename=filename,
        stored_name=stored_name,
        content_type=content_type,
        size_bytes=size_bytes,
    )
    session.add(doc)
    session.commit()
    session.refresh(doc)

    return doc


def delete_document(session: Session, doc: LoanDocument) -> None:
    session.delete(doc)
    session.commit()
