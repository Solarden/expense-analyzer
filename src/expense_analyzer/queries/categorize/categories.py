"""Category queries."""

import re

from sqlmodel import Session, col, select

from expense_analyzer.models import Category, CategoryKind

# A "#rrggbb" hex colour as emitted by <input type="color"> (Phase 16). The colour
# only ever feeds a CSS background / Chart.js fill, but we still pin the shape so a
# hand-crafted POST can't smuggle arbitrary text into the markup.
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def list_categories(session: Session) -> list[Category]:
    return list(session.exec(select(Category).order_by(col(Category.name))).all())


def get_category(session: Session, category_id: int) -> Category | None:
    return session.get(Category, category_id)


def create_category(
    session: Session, *, name: str, kind: CategoryKind, color: str | None = None
) -> Category:
    category = Category(name=name.strip(), kind=kind, color=color)
    session.add(category)
    session.commit()
    session.refresh(category)

    return category


def update_category(
    session: Session,
    category_id: int,
    *,
    name: str,
    kind: CategoryKind,
    color: str | None,
) -> Category | None:
    """Edit a category in place — rename, change kind, and set/clear the display
    colour (Phase 20b). Returns the updated category, or ``None`` if the id doesn't
    exist (the handler 404s). Changing ``kind`` reclassifies the category for stats
    (income/expense/transfer); transactions keep their ``category_id`` link."""
    category = session.get(Category, category_id)
    if category is None:
        return None

    category.name = name.strip()
    category.kind = kind
    category.color = color
    session.add(category)
    session.commit()
    session.refresh(category)

    return category
