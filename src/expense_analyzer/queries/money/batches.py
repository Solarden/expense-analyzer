"""Import-batch queries."""

from sqlmodel import Session, col, select

from expense_analyzer.models import ImportBatch


def recent_batches(session: Session, *, viewer_id: int | None = None) -> list[ImportBatch]:
    """Import batches, newest first — only the viewer's own.

    A batch's filename and record count describe rows the viewer may not be allowed
    to see, and its rollback button deletes them, so the list is scoped to the
    importer. ``viewer_id=None`` returns every batch (background/admin callers).
    """
    query = select(ImportBatch).order_by(col(ImportBatch.imported_at).desc())

    if viewer_id is not None:
        query = query.where(ImportBatch.owner_id == viewer_id)

    return list(session.exec(query).all())
