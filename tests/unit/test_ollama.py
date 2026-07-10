"""Ollama chat client tests — driven by an httpx MockTransport (no network)."""

import json
from collections.abc import Callable
from datetime import date

import httpx
import pytest

from expense_analyzer.ollama import LlmVerdict, OllamaClient, OllamaError

_CATEGORIES = [(1, "Groceries"), (2, "Fun")]

Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler) -> OllamaClient:
    return OllamaClient(
        base_url="http://piec:11434",
        model="gemma3:12b",
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )


def _categorize(client: OllamaClient) -> LlmVerdict:
    return client.categorize(
        merchant="BIEDRONKA", description="BIEDRONKA 123", amount=-5000, categories=_CATEGORIES
    )


def test_parses_structured_verdict() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        content = json.dumps({"category_id": 2, "confidence": 0.83})
        return httpx.Response(200, json={"message": {"content": content}})

    assert _categorize(_client(handler)) == LlmVerdict(category_id=2, confidence=0.83)


def test_sends_chat_request_with_model_and_schema() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        content = json.dumps({"category_id": 1, "confidence": 0.9})
        return httpx.Response(200, json={"message": {"content": content}})

    _categorize(_client(handler))

    assert seen["url"].endswith("/api/chat")
    assert seen["body"]["model"] == "gemma3:12b"
    assert seen["body"]["stream"] is False
    assert seen["body"]["format"]["required"] == ["category_id", "confidence"]


def test_http_error_raises_ollama_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="down")

    with pytest.raises(OllamaError):
        _categorize(_client(handler))


def test_non_json_content_raises_ollama_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": "not json at all"}})

    with pytest.raises(OllamaError):
        _categorize(_client(handler))


def test_missing_fields_raise_ollama_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Well-formed JSON but no category_id -> KeyError -> OllamaError.
        return httpx.Response(200, json={"message": {"content": json.dumps({"confidence": 0.5})}})

    with pytest.raises(OllamaError):
        _categorize(_client(handler))


def test_timeout_raises_ollama_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("piec is slow")

    with pytest.raises(OllamaError):
        _categorize(_client(handler))


def _content(obj: object) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": json.dumps(obj)}})

    return handler


# --- normalize_merchant ----------------------------------------------------


def test_normalize_merchant_parses_cleaned_name() -> None:
    client = _client(_content({"merchant": "Glovo"}))

    assert (
        client.normalize_merchant(raw_description="P24*GLOVO WAW", current="P24*GLOVO WAW")
        == "Glovo"
    )


def test_normalize_merchant_blank_reply_is_none() -> None:
    client = _client(_content({"merchant": "   "}))

    assert client.normalize_merchant(raw_description="x", current=None) is None


def test_normalize_merchant_wrong_type_raises() -> None:
    client = _client(_content({"merchant": 123}))

    with pytest.raises(OllamaError):
        client.normalize_merchant(raw_description="x", current=None)


# --- suggest_rule_patterns -------------------------------------------------


def test_suggest_rule_patterns_parses_and_trims() -> None:
    client = _client(_content({"patterns": ["BIEDRONKA", "  ", "LIDL "]}))

    result = client.suggest_rule_patterns(category_name="Groceries", examples=["BIEDRONKA 1"])

    assert result == ["BIEDRONKA", "LIDL"]  # blank dropped, whitespace trimmed


def test_suggest_rule_patterns_non_list_raises() -> None:
    client = _client(_content({"patterns": "nope"}))

    with pytest.raises(OllamaError):
        client.suggest_rule_patterns(category_name="X", examples=["a"])


def test_non_object_json_reply_raises() -> None:
    # A JSON array (not an object) must surface as OllamaError, not an AttributeError
    # from a caller's .get() — the guard lives in _chat_structured.
    client = _client(_content([1, 2, 3]))

    with pytest.raises(OllamaError):
        client.normalize_merchant(raw_description="x", current=None)


# --- parse_query -----------------------------------------------------------


def test_parse_query_returns_parsed_object() -> None:
    payload = {
        "category": "Groceries",
        "start_date": "2026-05-01",
        "end_date": "2026-05-31",
        "group_by": "category",
        "interpretation": "Groceries spending in May 2026",
    }
    client = _client(_content(payload))

    result = client.parse_query(
        "how much on groceries in may",
        categories=["Groceries", "Fun"],
        accounts=["PKO checking"],
        today=date(2026, 6, 1),
    )

    assert result == payload  # returned raw; the query layer validates it


def test_parse_query_sends_the_query_schema() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"message": {"content": json.dumps({"interpretation": "ok"})}}
        )

    _client(handler).parse_query("x", categories=["A"], accounts=["B"], today=date(2026, 1, 1))

    assert seen["body"]["format"]["required"] == ["interpretation"]
    assert "min_amount" in seen["body"]["format"]["properties"]
