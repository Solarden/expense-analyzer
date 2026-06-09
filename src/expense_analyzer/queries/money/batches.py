"""Import-batch queries."""

from sqlmodel import Session, col, select

from expense_analyzer.models import ImportBatch


def recent_batches(session: Session) -> list[ImportBatch]:
    return list(
        session.exec(select(ImportBatch).order_by(col(ImportBatch.imported_at).desc())).all()
    )
