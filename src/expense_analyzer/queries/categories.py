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


def set_category_color(session: Session, category_id: int, color: str | None) -> Category | None:
    """Set (or clear, with ``None``) a category's display colour. Returns the
    updated category, or ``None`` if the id doesn't exist (the handler 404s/flashes)."""
    category = session.get(Category, category_id)
    if category is None:
        return None

    category.color = color
    session.add(category)
    session.commit()
    session.refresh(category)

    return category
