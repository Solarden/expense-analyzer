"""Lenient coercion of optional query/form string params.

The dashboard's filter bars and ``?edit=`` links auto-submit raw strings, and a
URL can be hand-edited, so handlers parse each param leniently — an empty or
invalid value falls back to a default rather than 422/500-ing the page.
"""


def opt_int(value: str | None) -> int | None:
    """Coerce an optional param string to an int, or None when it isn't one.

    Returns None for None, ``""``, and any non-numeric text. Crucially, the
    ``isascii()`` guard also rejects numeric-looking strings ``int()`` can't parse
    — Unicode digits that ``str.isdigit()`` accepts but ``int()`` raises on (e.g.
    the superscript ``"²"``). Without it a crafted ``?param=²`` would raise an
    unhandled ``ValueError`` (an HTTP 500) instead of falling back to the default.
    """
    return int(value) if value and value.isascii() and value.isdigit() else None
