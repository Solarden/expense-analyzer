"""Ollama chat client (piec) — the primary transaction categorizer.

The owner runs Ollama on *piec*, a capable LAN box, so heavy categorization
doesn't tax the Pi. This is a thin, sync client over Ollama's ``/api/chat`` with
JSON-schema structured output; the local sklearn classifier stays the fallback
for when piec is unreachable (see :mod:`expense_analyzer.queries.categorize.llm`).

OPT-IN and OFF by default (``EA_LLM_ENABLED`` + ``EA_LLM_BASE_URL``): with the
feature off the client is never constructed. piec is on the LAN, so this is
*not* internet egress (same footing as the MQTT broker) — nothing leaves the
house.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass

import httpx

from expense_analyzer.config import Settings

_ENDPOINT = "/api/chat"
# A down piec should fail *fast* (connection refused is immediate); only a piec
# that accepts the socket but is slow to infer should ride the longer read budget.
# So split the timeout: short connect, tunable read.
# ponytail: connect timeout hard-coded; read timeout is the tunable knob (llm_timeout).
_CONNECT_TIMEOUT = 3.0

# Structured-output contract: Ollama constrains the model to this JSON shape
# (a llama.cpp grammar built from the schema), so we get parseable output, not prose.
_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "category_id": {"type": "integer"},
        "confidence": {"type": "number"},
    },
    "required": ["category_id", "confidence"],
}


class OllamaError(Exception):
    """A call to piec's Ollama failed (unreachable, timeout, or unusable output).

    This is the signal for the categorization layer to fall back to the local
    classifier.
    """


@dataclass(frozen=True, slots=True)
class LlmVerdict:
    category_id: int
    confidence: float  # 0..1, the model's self-reported confidence


class OllamaClient:
    """Categorizes a single transaction via piec's Ollama chat API.

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

    def categorize(
        self,
        *,
        merchant: str | None,
        description: str,
        amount: int,
        categories: Sequence[tuple[int, str]],
    ) -> LlmVerdict:
        """Ask piec for the best category id + confidence for one transaction.

        Raises :class:`OllamaError` on any transport/HTTP/decode failure — the
        signal to fall back to the local classifier. A well-formed response with
        an out-of-range ``category_id`` is *not* handled here: the verdict is
        returned as-is and the caller validates the id against the real category set.
        """
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _system_prompt(categories)},
                {"role": "user", "content": _transaction_prompt(merchant, description, amount)},
            ],
            "format": _RESPONSE_SCHEMA,
            "stream": False,
            # Deterministic: classification wants the argmax, not a sampled guess.
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
                content = response.json()["message"]["content"]
                verdict = json.loads(content)
                return LlmVerdict(
                    category_id=int(verdict["category_id"]),
                    confidence=float(verdict["confidence"]),
                )
        except httpx.HTTPError as exc:
            raise OllamaError(f"Ollama request failed: {type(exc).__name__}") from exc
        except (KeyError, ValueError, TypeError) as exc:
            # Missing message/content, a non-JSON body, or wrong-typed fields.
            raise OllamaError(f"Ollama returned unusable output: {type(exc).__name__}") from exc


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
    # Amount is signed minor units (negative = expense); present it as a decimal
    # with direction so the model has the sign as a hint.
    # ponytail: assumes 2-decimal minor units (PLN/EUR/USD); fine for this app.
    direction = "expense" if amount < 0 else "income"
    value = f"{abs(amount) / 100:.2f}"
    label = merchant or description
    return (
        f"Merchant/description: {label}\n"
        f"Raw description: {description}\n"
        f"Amount: {value} ({direction})"
    )
