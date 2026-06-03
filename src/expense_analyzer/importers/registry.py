"""Registry of available bank importers, keyed by a short slug.

The dashboard upload form lists whatever is registered here, so adding a new
bank is one ``register()`` call — no dashboard changes. This is the seam the
design's "build around the importer" principle (§1) buys us.
"""

from expense_analyzer.importers.base import Importer
from expense_analyzer.importers.pko import PKOCsvImporter

_REGISTRY: dict[str, Importer] = {}


def register(slug: str, importer: Importer) -> None:
    _REGISTRY[slug] = importer


def get_importer(slug: str) -> Importer:
    try:
        return _REGISTRY[slug]
    except KeyError as exc:
        raise ValueError(f"unknown importer: {slug!r}") from exc


def available() -> dict[str, Importer]:
    """All registered importers, slug -> instance (used by the upload form)."""
    return dict(_REGISTRY)


# Bank parsers shipped in the box.
register("pko", PKOCsvImporter())
