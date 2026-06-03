"""Category queries."""

from sqlmodel import Session, col, select

from expense_analyzer.models import Category, CategoryKind


def list_categories(session: Session) -> list[Category]:
    return list(session.exec(select(Category).order_by(col(Category.name))).all())


def get_category(session: Session, category_id: int) -> Category | None:
    return session.get(Category, category_id)


def create_category(session: Session, *, name: str, kind: CategoryKind) -> Category:
    category = Category(name=name.strip(), kind=kind)
    session.add(category)
    session.commit()
    session.refresh(category)

    return category
