"""Ollama chat client (piec) — the primary transaction categorizer, plus two
human-in-the-loop helpers (merchant normalization + rule suggestions).

The owner runs Ollama on *piec*, a capable LAN box, so heavy LLM work doesn't tax
the Pi. This is a thin, sync client over Ollama's ``/api/chat`` with JSON-schema
structured output; the local sklearn classifier stays the fallback for when piec
is unreachable (see :mod:`expense_analyzer.queries.categorize.llm`).

OPT-IN and OFF by default (``EA_LLM_ENABLED`` + ``EA_LLM_BASE_URL``): with the
feature off the client is never constructed. piec is on the LAN, so this is *not*
internet egress (same footing as the MQTT broker) — nothing leaves the house.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import httpx

from expense_analyzer.config import Settings

_ENDPOINT = "/api/chat"
# Short connect timeout so a down piec fails fast (connection refused is
# immediate); the longer read budget (llm_timeout) covers slow inference.
_CONNECT_TIMEOUT = 3.0

# Structured-output contracts: Ollama constrains the model to these JSON shapes
# (a llama.cpp grammar built from the schema), so we get parseable output, not prose.
_CATEGORIZE_SCHEMA = {
    "type": "object",
    "properties": {
        "category_id": {"type": "integer"},
        "confidence": {"type": "number"},
    },
    "required": ["category_id", "confidence"],
}
_MERCHANT_SCHEMA = {
    "type": "object",
    "properties": {"merchant": {"type": "string"}},
    "required": ["merchant"],
}
_RULES_SCHEMA = {
    "type": "object",
    "properties": {"patterns": {"type": "array", "items": {"type": "string"}}},
    "required": ["patterns"],
}
# Natural-language query filter. Every field but ``interpretation`` is nullable —
# the model fills only what it's sure of. This is a *hint* to the grammar, not the
# trust boundary: :func:`expense_analyzer.queries.money.nl_query.build_spec`
# re-validates and resolves every field server-side before anything is used.
_QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": ["string", "null"]},
        "account": {"type": ["string", "null"]},
        "start_date": {"type": ["string", "null"]},  # ISO YYYY-MM-DD
        "end_date": {"type": ["string", "null"]},
        "min_amount": {"type": ["number", "null"]},  # main units (zł)
        "max_amount": {"type": ["number", "null"]},
        "direction": {"type": ["string", "null"]},  # expense | income
        "group_by": {"type": ["string", "null"]},  # category | month
        "interpretation": {"type": "string"},
    },
    "required": ["interpretation"],
}


class OllamaError(Exception):
    """A call to piec's Ollama failed (unreachable, timeout, or unusable output).

    For categorization this is the signal to fall back to the local classifier;
    for the (optional) merchant/rule helpers it just aborts that batch.
    """


@dataclass(frozen=True, slots=True)
class LlmVerdict:
    category_id: int
    confidence: float  # 0..1, the model's self-reported confidence


class OllamaClient:
    """Talks to piec's Ollama chat API for the LLM features.

    ``transport`` is injectable so tests can drive it with an
    :class:`httpx.MockTransport` instead of hitting piec.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = httpx.Timeout(timeout, connect=_CONNECT_TIMEOUT)
        self._transport = transport

    @classmethod
    def from_settings(
        cls, settings: Settings, *, transport: httpx.BaseTransport | None = None
    ) -> "OllamaClient":
        return cls(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            timeout=float(settings.llm_timeout),
            transport=transport,
        )

    def _chat_structured(self, *, system: str, user: str, schema: dict) -> dict:
        """One chat turn with JSON-schema structured output → the parsed object.

        The single HTTP path shared by every feature. Raises :class:`OllamaError`
        on any transport/HTTP/decode failure.
        """
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "format": schema,
            "stream": False,
            # Deterministic: these tasks want the argmax, not a sampled guess.
            "options": {"temperature": 0},
        }
        try:
            with httpx.Client(
                base_url=self._base_url,
                timeout=self._timeout,
                transport=self._transport,
                follow_redirects=False,
            ) as client:
                response = client.post(_ENDPOINT, json=payload)
                response.raise_for_status()
                parsed = json.loads(response.json()["message"]["content"])
        except httpx.HTTPError as exc:
            raise OllamaError(f"Ollama request failed: {type(exc).__name__}") from exc
        except (KeyError, ValueError, TypeError) as exc:
            # Missing message/content, or a non-JSON body.
            raise OllamaError(f"Ollama returned unusable output: {type(exc).__name__}") from exc

        # `format` should guarantee an object, but a non-dict reply (e.g. a JSON
        # array) would blow up the callers' .get()/[] access with an uncaught
        # error — normalize it to OllamaError here so every caller is covered.
        if not isinstance(parsed, dict):
            raise OllamaError("Ollama returned unusable output: expected a JSON object")

        return parsed

    def categorize(
        self,
        *,
        merchant: str | None,
        description: str,
        amount: int,
        categories: Sequence[tuple[int, str]],
    ) -> LlmVerdict:
        """Ask piec for the best category id + confidence for one transaction.

        Raises :class:`OllamaError` on transport/decode failure — the signal to
        fall back to the local classifier. A well-formed response with an
        out-of-range ``category_id`` is *not* handled here: the verdict is returned
        as-is and the caller validates the id against the real category set.
        """
        verdict = self._chat_structured(
            system=_system_prompt(categories),
            user=_transaction_prompt(merchant, description, amount),
            schema=_CATEGORIZE_SCHEMA,
        )
        try:
            return LlmVerdict(
                category_id=int(verdict["category_id"]),
                confidence=float(verdict["confidence"]),
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise OllamaError(f"Ollama returned unusable output: {type(exc).__name__}") from exc

    def normalize_merchant(self, *, raw_description: str, current: str | None) -> str | None:
        """Ask piec for a short, clean merchant name for one transaction.

        Returns the cleaned name, or ``None`` when the model has nothing better (an
        empty reply). Raises :class:`OllamaError` on transport/decode failure.
        """
        result = self._chat_structured(
            system=_MERCHANT_PROMPT,
            user=_merchant_prompt(raw_description, current),
            schema=_MERCHANT_SCHEMA,
        )
        merchant = result.get("merchant")
        if not isinstance(merchant, str):
            raise OllamaError("Ollama returned unusable output: merchant not a string")

        return merchant.strip() or None

    def suggest_rule_patterns(self, *, category_name: str, examples: Sequence[str]) -> list[str]:
        """Ask piec for substring rule patterns that would match these examples.

        Returns a (possibly empty) list of trimmed, non-blank patterns. Raises
        :class:`OllamaError` on transport/decode failure.
        """
        result = self._chat_structured(
            system=_RULES_PROMPT,
            user=_rules_prompt(category_name, examples),
            schema=_RULES_SCHEMA,
        )
        patterns = result.get("patterns")
        if not isinstance(patterns, list):
            raise OllamaError("Ollama returned unusable output: patterns not a list")

        return [p.strip() for p in patterns if isinstance(p, str) and p.strip()]

    def parse_query(
        self, question: str, *, categories: Sequence[str], accounts: Sequence[str], today: date
    ) -> dict:
        """Turn a natural-language spending question into a raw structured-filter dict.

        Categories and accounts are given to the model *by name* so it never emits
        ids (let alone SQL). Returns the raw parsed object as-is; the query layer
        (:mod:`expense_analyzer.queries.money.nl_query`) is the validation boundary
        that resolves names → ids and rejects anything malformed. Raises
        :class:`OllamaError` on transport/decode failure.
        """
        return self._chat_structured(
            system=_query_prompt(categories, accounts, today),
            user=question,
            schema=_QUERY_SCHEMA,
        )


def _system_prompt(categories: Sequence[tuple[int, str]]) -> str:
    lines = "\n".join(f"{cid}: {name}" for cid, name in categories)
    return (
        "You categorize a single bank transaction into exactly one category.\n"
        "Choose the best match from this list (id: name):\n"
        f"{lines}\n\n"
        "Reply with the chosen category id and your confidence from 0 to 1. "
        "If nothing fits well, still pick the closest and give a low confidence."
    )


def _transaction_prompt(merchant: str | None, description: str, amount: int) -> str:
    # Amount is signed minor units (negative = expense); show it as a decimal with
    # direction. Assumes 2-decimal minor units (PLN/EUR/USD), which this app uses.
    direction = "expense" if amount < 0 else "income"
    value = f"{abs(amount) / 100:.2f}"
    label = merchant or description
    return (
        f"Merchant/description: {label}\n"
        f"Raw description: {description}\n"
        f"Amount: {value} ({direction})"
    )


_MERCHANT_PROMPT = (
    "You clean a bank transaction description into a short, human-friendly merchant "
    "name (e.g. 'P24*GLOVO WAW 12345' -> 'Glovo', 'PAYPAL *SPOTIFY' -> 'Spotify'). "
    "Drop payment-processor prefixes, city/branch codes, and card/reference numbers; "
    "keep the recognisable brand. If you can't do better than what's given, return "
    "an empty string."
)


def _merchant_prompt(raw_description: str, current: str | None) -> str:
    return (
        f"Raw description: {raw_description}\n"
        f"Current name: {current or '(none)'}\n"
        "Return the cleaned merchant name."
    )


_RULES_PROMPT = (
    "You propose short case-insensitive substring patterns for auto-categorizing "
    "bank transactions. Given example merchant strings that all belong to one "
    "category, return the minimal distinguishing substrings (e.g. from "
    "'BIEDRONKA 123' and 'BIEDRONKA 456' -> 'BIEDRONKA'). Prefer a few general "
    "patterns over many specific ones, and return an empty list if unsure."
)


def _rules_prompt(category_name: str, examples: Sequence[str]) -> str:
    joined = "\n".join(f"- {e}" for e in examples)
    return f"Category: {category_name}\nExample merchant strings:\n{joined}"


def _query_prompt(categories: Sequence[str], accounts: Sequence[str], today: date) -> str:
    cats = ", ".join(categories) or "(none)"
    accs = ", ".join(accounts) or "(none)"
    return (
        "You turn a question about personal spending into a structured filter. "
        f"Today is {today.isoformat()}.\n"
        "Fill only the fields you are confident about; leave the rest null.\n"
        f"Known categories (use an exact name from here, or null): {cats}\n"
        f"Known accounts (use an exact name from here, or null): {accs}\n"
        "Fields:\n"
        "- category / account: an exact name from the lists above, or null. "
        "Never invent a name that is not listed.\n"
        "- start_date / end_date: ISO YYYY-MM-DD bounds (inclusive), or null. "
        "Resolve relative ranges like 'last month' or 'this year' against today.\n"
        "- min_amount / max_amount: bounds on the transaction amount in złoty "
        "(main units, e.g. 100 means 100 zł), or null. Compares the magnitude.\n"
        "- direction: 'expense' or 'income', or null.\n"
        "- group_by: 'category' or 'month' if a breakdown is asked for, else null.\n"
        "- interpretation: one short sentence restating what you understood."
    )
